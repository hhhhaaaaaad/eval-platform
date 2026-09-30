"""数据集与版本服务（EP-4）。

**不可变性是这一层的核心约束**，不是靠「不提供 update 接口」这种约定，而是三条硬规则：

1. 版本只能**追加**：没有改版本的方法，也没有删版本的方法。发现标注错了必须导新版本——
   这样「某个历史指标是在哪份标注上算出来的」永远可回溯。
2. 已归档数据集**拒绝导入**：归档语义是「封存，不再演进」。
3. 同数据集内 **content_digest 相同的版本拒绝重复导入**：同一批标注导入两次是无意义的
   版本膨胀，且会让「版本号」失去指示演进顺序的作用。

失败的写入必须整体回滚（版本行 + 全部 case 行），所以这里只 flush 不 commit，
由调用方（API 层）决定事务边界——与 `app/auth/api.py` 的既有约定一致。
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.audit.service import write_audit
from app.datasets.digest import case_content_hash, version_content_digest
from app.datasets.models import Case, Dataset, DatasetVersion
from app.datasets.schemas import (
    SUPPORTED_SCHEMA_VERSION,
    CaseInput,
    DatasetVersionImportRequest,
)
from app.settings.logging import get_logger

logger = get_logger(__name__)


class DatasetError(Exception):
    """数据集领域错误基类。"""


class DatasetNotFoundError(DatasetError):
    pass


class VersionNotFoundError(DatasetError):
    pass


class DatasetArchivedError(DatasetError):
    """已归档数据集拒绝写入（不可变性规则 2）。"""


class VersionAlreadyExistsError(DatasetError):
    """同数据集下版本号重复。"""


class DuplicateCaseError(DatasetError):
    """同一批导入里出现重复用例（content_hash 撞车）。"""

    def __init__(self, content_hash: str, indexes: list[int]) -> None:
        super().__init__(
            f"批次内存在重复用例: content_hash={content_hash} 出现在第 {indexes} 条——"
            "同一版本内 content_hash 必须唯一"
        )
        self.content_hash = content_hash
        self.indexes = indexes


class DuplicateDigestError(DatasetError):
    """同数据集下已存在内容完全相同的版本（含 ground truth）。"""

    def __init__(self, content_digest: str, existing_version_id: int) -> None:
        super().__init__(
            f"该数据集已存在内容相同的版本 id={existing_version_id}"
            f"（content_digest={content_digest}）——如需修正标注请修改内容后重新导入"
        )
        self.content_digest = content_digest
        self.existing_version_id = existing_version_id


class ParentVersionError(DatasetError):
    """parent_version_id 不属于同一数据集，谱系会串到别的数据集上。"""


class DatasetService:
    """数据集与版本的读写入口。"""

    def __init__(self, db: Session) -> None:
        self._db = db

    # -- 数据集 CRUD ------------------------------------------------------

    def create_dataset(
        self, *, name: str, description: str | None, created_by: int | None
    ) -> Dataset:
        dataset = Dataset(name=name, description=description, created_by=created_by)
        self._db.add(dataset)
        self._db.flush()
        logger.info("创建数据集 id=%s name=%s", dataset.id, name)
        return dataset

    def list_datasets(self, *, include_archived: bool = False) -> list[Dataset]:
        stmt = select(Dataset).order_by(Dataset.id)
        if not include_archived:
            stmt = stmt.where(Dataset.is_archived.is_(False))
        return list(self._db.execute(stmt).scalars())

    def get_dataset(self, dataset_id: int) -> Dataset:
        dataset = self._db.get(Dataset, dataset_id)
        if dataset is None:
            raise DatasetNotFoundError(f"数据集不存在: id={dataset_id}")
        return dataset

    def archive_dataset(self, dataset_id: int) -> Dataset:
        """归档数据集。幂等：重复归档不报错。

        **只置标志，不删数据**——历史版本的 run 结果还指着它。
        """
        dataset = self.get_dataset(dataset_id)
        if not dataset.is_archived:
            dataset.is_archived = True
            self._db.flush()
            logger.info("归档数据集 id=%s", dataset_id)
        return dataset

    # -- 版本导入 ---------------------------------------------------------

    def import_version(
        self,
        dataset_id: int,
        request: DatasetVersionImportRequest,
        *,
        created_by: int | None,
    ) -> DatasetVersion:
        """导入一个新版本（case 已在 pydantic 层通过逐类型校验）。

        步骤与顺序都是有意的：

        1. 归档检查放在最前——避免做完全部计算才被拒；
        2. 批次内去重先于 digest 计算——重复用例会让 digest 虚高且掩盖真实规模；
        3. digest 重复检查放在插入前——靠它实现「同一批标注不重复入库」。
        """
        dataset = self.get_dataset(dataset_id)
        if dataset.is_archived:
            raise DatasetArchivedError(f"数据集已归档，不能再导入新版本: id={dataset_id}")

        self._ensure_version_number_free(dataset_id, request.version)
        self._ensure_parent_in_same_dataset(dataset_id, request.parent_version_id)

        case_rows = self._build_case_rows(request.cases)
        # case_rows 已含 content_hash / payload / ground_truth，digest 需要的字段齐全。
        content_digest = version_content_digest(case_rows)
        self._ensure_digest_is_new(dataset_id, content_digest)

        version = DatasetVersion(
            dataset_id=dataset_id,
            version=request.version,
            schema_version=SUPPORTED_SCHEMA_VERSION,
            source=request.source,
            parent_version_id=request.parent_version_id,
            content_digest=content_digest,
            config=request.config,
            created_by=created_by,
        )
        self._db.add(version)
        self._db.flush()

        for row in case_rows:
            self._db.add(
                Case(
                    dataset_version_id=version.id,
                    case_type=row["case_type"],
                    group_key=row["group_key"],
                    content_hash=row["content_hash"],
                    payload=row["payload"],
                    ground_truth=row["ground_truth"],
                )
            )

        try:
            self._db.flush()
        except IntegrityError as exc:
            # 竞态兜底：两个请求同时通过上面的 service 层检查时，由
            # (dataset_version_id, content_hash) 唯一索引与 (dataset_id, version)
            # 唯一约束挡住。转成领域错误，让 API 层返回 409 而不是 500。
            self._db.rollback()
            raise VersionAlreadyExistsError(
                f"并发导入冲突：版本 {request.version} 或其中用例已被写入"
            ) from exc

        logger.info(
            "导入数据集版本 dataset_id=%s version=%s cases=%d digest=%s",
            dataset_id,
            request.version,
            len(case_rows),
            content_digest,
        )
        return version

    def _ensure_version_number_free(self, dataset_id: int, version: str) -> None:
        exists = self._db.execute(
            select(DatasetVersion.id).where(
                DatasetVersion.dataset_id == dataset_id,
                DatasetVersion.version == version,
            )
        ).scalar_one_or_none()
        if exists is not None:
            raise VersionAlreadyExistsError(
                f"数据集 id={dataset_id} 下版本 {version} 已存在（id={exists}）——版本只能追加，不能覆盖"
            )

    def _ensure_parent_in_same_dataset(self, dataset_id: int, parent_version_id: int | None) -> None:
        if parent_version_id is None:
            return
        parent = self._db.get(DatasetVersion, parent_version_id)
        if parent is None or parent.dataset_id != dataset_id:
            raise ParentVersionError(
                f"parent_version_id={parent_version_id} 不属于数据集 id={dataset_id}"
            )

    def _ensure_digest_is_new(self, dataset_id: int, content_digest: str) -> None:
        existing = self._db.execute(
            select(DatasetVersion.id).where(
                DatasetVersion.dataset_id == dataset_id,
                DatasetVersion.content_digest == content_digest,
            )
        ).scalar_one_or_none()
        if existing is not None:
            raise DuplicateDigestError(content_digest, existing)

    @staticmethod
    def _build_case_rows(cases: list[CaseInput]) -> list[dict[str, object]]:
        """算 content_hash 并做批次内去重。返回顺序与输入一致。"""
        rows: list[dict[str, object]] = []
        seen: dict[str, list[int]] = {}

        for index, case in enumerate(cases):
            content_hash = case_content_hash(case.case_type, case.payload)
            seen.setdefault(content_hash, []).append(index)
            rows.append(
                {
                    "case_type": case.case_type,
                    "group_key": case.group_key,
                    "content_hash": content_hash,
                    "payload": case.payload,
                    "ground_truth": case.ground_truth,
                }
            )

        duplicated = {h: idx for h, idx in seen.items() if len(idx) > 1}
        if duplicated:
            # 只报第一个重复项：一次修一条比糊一屏更有用。
            content_hash, indexes = next(iter(duplicated.items()))
            raise DuplicateCaseError(content_hash, indexes)
        return rows

    # -- 版本查询 ---------------------------------------------------------

    def list_versions(self, dataset_id: int) -> list[tuple[DatasetVersion, int]]:
        """列出某数据集的全部版本，附带 case 数。"""
        self.get_dataset(dataset_id)  # 不存在则报错，而不是返回空列表

        case_count = func.count(Case.id).label("case_count")
        stmt = (
            select(DatasetVersion, case_count)
            .outerjoin(Case, Case.dataset_version_id == DatasetVersion.id)
            .where(DatasetVersion.dataset_id == dataset_id)
            .group_by(DatasetVersion.id)
            .order_by(DatasetVersion.id)
        )
        return [(version, count) for version, count in self._db.execute(stmt).all()]

    def get_version(self, version_id: int) -> tuple[DatasetVersion, list[Case]]:
        version = self._db.get(DatasetVersion, version_id)
        if version is None:
            raise VersionNotFoundError(f"数据集版本不存在: id={version_id}")
        cases = list(
            self._db.execute(
                select(Case).where(Case.dataset_version_id == version_id).order_by(Case.id)
            ).scalars()
        )
        return version, cases


def audit_dataset_change(
    db: Session,
    *,
    actor_user_id: int | None,
    action: str,
    resource_type: str,
    resource_id: str,
    after: dict[str, object] | None = None,
    ip: str | None = None,
) -> None:
    """数据集模块的审计写入薄封装，统一资源类型命名。"""
    write_audit(
        db,
        actor_user_id=actor_user_id,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        after=after,
        ip=ip,
    )
