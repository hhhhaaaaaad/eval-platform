"""数据集服务的**真实 Postgres** 集成测试（EP-4 出口条件）。

只跑单测是不够的：这层的行为有三处**只有真库才能验证**——

- ``(dataset_version_id, content_hash)`` 唯一索引是否真的挡得住重复写；
- 导入失败时版本行与 case 行是否**一起**回滚（不能留下半个版本）；
- ``ON DELETE CASCADE`` 是否真的随数据集删除而清理版本与用例。

不可用时整模块跳过（``pytest.skip``），无数据库的环境 ``pytest`` 仍全绿——
与 ``test_schema_constraints.py`` 的门控同理。

服务层按设计**只 flush 不 commit**，所以每个用例结束 ``rollback`` 即可清场，
不必手工删数据；唯一例外是那条故意触发 IntegrityError 的用例，它用 savepoint 隔离。
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.datasets.digest import case_content_hash
from app.datasets.models import Case, DatasetVersion
from app.datasets.schemas import CaseInput, DatasetVersionImportRequest
from app.datasets.service import (
    DatasetArchivedError,
    DatasetNotFoundError,
    DatasetService,
    DuplicateCaseError,
    DuplicateDigestError,
    ParentVersionError,
    VersionAlreadyExistsError,
)
from app.db.session import get_session_factory


@pytest.fixture
def db() -> Iterator[Session]:
    try:
        session = get_session_factory()()
        session.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001 — 探活失败即跳过，不给噪声
        pytest.skip(f"Postgres 不可用，跳过数据集服务集成测试: {exc}")
    yield session
    # 服务层不 commit，回滚即可撤销本用例的全部写入。
    session.rollback()
    session.close()


def _make_dataset(service: DatasetService, *, name: str | None = None) -> int:
    suffix = uuid.uuid4().hex[:8]
    dataset = service.create_dataset(
        name=name or f"ds-{suffix}", description=None, created_by=None
    )
    return dataset.id


def _query_case(query_id: str, *, query: str = "咖啡", memory_ids: list[int] | None = None) -> CaseInput:
    return CaseInput(
        case_type="query_to_memory",
        group_key="retrieval-basic",
        payload={"query_id": query_id, "query": query, "task_type": "LEGACY"},
        ground_truth={"relevant_memory_ids": memory_ids if memory_ids is not None else [101]},
    )


def _conversation_case(dialogue_id: str = "d001") -> CaseInput:
    return CaseInput(
        case_type="conversation_to_memory",
        group_key="extraction-basic",
        payload={
            "dialogue_id": dialogue_id,
            "messages": [{"role": "user", "content": "我们项目用 Java 17"}],
        },
        ground_truth={
            "ground_truth_memories": [
                {"content": "技术栈 Java 17", "type": "fact", "attributed_to": "user"}
            ]
        },
    )


def _import_request(version: str, cases: list[CaseInput], **overrides) -> DatasetVersionImportRequest:
    payload = {
        "version": version,
        "source": "manual",
        "cases": cases,
    }
    payload.update(overrides)
    return DatasetVersionImportRequest(**payload)


# ---------------------------------------------------------------------------
# 基本导入
# ---------------------------------------------------------------------------


class TestImportVersion:
    def test_persists_version_and_cases(self, db: Session) -> None:
        service = DatasetService(db)
        dataset_id = _make_dataset(service)

        version = service.import_version(
            dataset_id, _import_request("v1", [_query_case("q1"), _conversation_case()]), created_by=None
        )

        assert version.id is not None
        assert version.schema_version == 1
        assert version.content_digest.startswith("sha256:")

        _, cases = service.get_version(version.id)
        assert len(cases) == 2
        assert {case.case_type for case in cases} == {"query_to_memory", "conversation_to_memory"}

    def test_content_hash_is_unique_within_version_at_db_level(self, db: Session) -> None:
        """**EP-4 验收条款**：同一 dataset version 下 content_hash 唯一。

        绕过服务层的批次去重，直接写两行相同 content_hash，验证是**数据库**在兜底——
        服务层检查挡不住并发，唯一索引才是最终防线。
        """
        service = DatasetService(db)
        dataset_id = _make_dataset(service)
        version = service.import_version(dataset_id, _import_request("v1", [_query_case("q1")]), created_by=None)

        payload = {"query_id": "q1", "query": "咖啡", "task_type": "LEGACY"}
        content_hash = case_content_hash("query_to_memory", payload)

        # savepoint 隔离：IntegrityError 会污染当前事务，包一层 nested 让外层继续可用。
        with pytest.raises(IntegrityError), db.begin_nested():
            db.add(
                Case(
                    dataset_version_id=version.id,
                    case_type="query_to_memory",
                    group_key="dup",
                    content_hash=content_hash,
                    payload=payload,
                    ground_truth={},
                )
            )
            db.flush()

    def test_list_versions_reports_case_count(self, db: Session) -> None:
        service = DatasetService(db)
        dataset_id = _make_dataset(service)
        service.import_version(dataset_id, _import_request("v1", [_query_case("q1")]), created_by=None)
        service.import_version(
            dataset_id, _import_request("v2", [_query_case("q1"), _query_case("q2")]), created_by=None
        )

        rows = service.list_versions(dataset_id)
        assert [(version.version, count) for version, count in rows] == [("v1", 1), ("v2", 2)]


# ---------------------------------------------------------------------------
# 不可变性
# ---------------------------------------------------------------------------


class TestImmutability:
    def test_version_number_cannot_be_reused(self, db: Session) -> None:
        """版本只能追加，不能覆盖。"""
        service = DatasetService(db)
        dataset_id = _make_dataset(service)
        service.import_version(dataset_id, _import_request("v1", [_query_case("q1")]), created_by=None)

        with pytest.raises(VersionAlreadyExistsError) as excinfo:
            service.import_version(
                dataset_id, _import_request("v1", [_query_case("q2")]), created_by=None
            )
        assert "只能追加" in str(excinfo.value)

    def test_archived_dataset_rejects_import(self, db: Session) -> None:
        """**EP-4 验收条款**：已归档版本不能被覆盖。"""
        service = DatasetService(db)
        dataset_id = _make_dataset(service)
        service.import_version(dataset_id, _import_request("v1", [_query_case("q1")]), created_by=None)
        service.archive_dataset(dataset_id)

        with pytest.raises(DatasetArchivedError):
            service.import_version(dataset_id, _import_request("v2", [_query_case("q2")]), created_by=None)

    def test_archive_is_idempotent_and_history_survives(self, db: Session) -> None:
        """归档是封存而非删除：历史版本必须还在。"""
        service = DatasetService(db)
        dataset_id = _make_dataset(service)
        service.import_version(dataset_id, _import_request("v1", [_query_case("q1")]), created_by=None)

        service.archive_dataset(dataset_id)
        service.archive_dataset(dataset_id)  # 重复归档不报错

        assert len(service.list_versions(dataset_id)) == 1

    def test_archived_dataset_is_hidden_by_default(self, db: Session) -> None:
        service = DatasetService(db)
        dataset_id = _make_dataset(service)
        service.archive_dataset(dataset_id)

        visible = [dataset.id for dataset in service.list_datasets()]
        assert dataset_id not in visible
        assert dataset_id in [dataset.id for dataset in service.list_datasets(include_archived=True)]


# ---------------------------------------------------------------------------
# 去重与 digest
# ---------------------------------------------------------------------------


class TestDigestAndDedupe:
    def test_duplicate_cases_in_one_batch_are_rejected(self, db: Session) -> None:
        """同一批次里两条一模一样的 case 必须报错，而不是悄悄写两行。"""
        service = DatasetService(db)
        dataset_id = _make_dataset(service)

        with pytest.raises(DuplicateCaseError) as excinfo:
            service.import_version(
                dataset_id,
                _import_request("v1", [_query_case("q1"), _query_case("q1")]),
                created_by=None,
            )
        assert excinfo.value.indexes == [0, 1]

    def test_identical_content_under_new_version_is_rejected(self, db: Session) -> None:
        """内容完全相同的版本重复导入是无意义的版本膨胀，必须拒绝。"""
        service = DatasetService(db)
        dataset_id = _make_dataset(service)
        first = service.import_version(dataset_id, _import_request("v1", [_query_case("q1")]), created_by=None)

        with pytest.raises(DuplicateDigestError) as excinfo:
            service.import_version(dataset_id, _import_request("v2", [_query_case("q1")]), created_by=None)

        assert excinfo.value.existing_version_id == first.id

    def test_changing_ground_truth_changes_digest_and_is_accepted(self, db: Session) -> None:
        """**EP-4 验收条款**：修改 ground truth 会改变 content_digest。

        输入完全相同、只改标注 —— 必须被当成一个**新版本**接受，
        且 digest 与旧版本不同。这正是「同一批 query、不同答案」不能混淆的地方。
        """
        service = DatasetService(db)
        dataset_id = _make_dataset(service)

        v1 = service.import_version(
            dataset_id, _import_request("v1", [_query_case("q1", memory_ids=[101])]), created_by=None
        )
        v2 = service.import_version(
            dataset_id, _import_request("v2", [_query_case("q1", memory_ids=[101, 102])]), created_by=None
        )

        assert v1.content_digest != v2.content_digest
        assert v2.id != v1.id

    def test_group_key_change_is_a_new_version(self, db: Session) -> None:
        """换个分组标签不参与 content_hash（否则去重失效），但要改 digest。"""
        service = DatasetService(db)
        dataset_id = _make_dataset(service)

        v1 = service.import_version(dataset_id, _import_request("v1", [_query_case("q1")]), created_by=None)
        regrouped = _query_case("q1")
        regrouped.group_key = "retrieval-hard"
        v2 = service.import_version(dataset_id, _import_request("v2", [regrouped]), created_by=None)

        assert v1.content_digest != v2.content_digest

    def test_same_content_allowed_in_different_datasets(self, db: Session) -> None:
        """去重是**数据集内**的：两个数据集各自持有同一批判注是合法的。"""
        service = DatasetService(db)
        a = _make_dataset(service)
        b = _make_dataset(service)

        service.import_version(a, _import_request("v1", [_query_case("q1")]), created_by=None)
        service.import_version(b, _import_request("v1", [_query_case("q1")]), created_by=None)


# ---------------------------------------------------------------------------
# 谱系与错误路径
# ---------------------------------------------------------------------------


class TestLineageAndErrors:
    def test_parent_version_must_belong_to_same_dataset(self, db: Session) -> None:
        """谱系不能跨数据集，否则「这个版本从哪来」会指向别的评测集。"""
        service = DatasetService(db)
        a = _make_dataset(service)
        b = _make_dataset(service)

        parent = service.import_version(a, _import_request("v1", [_query_case("q1")]), created_by=None)

        with pytest.raises(ParentVersionError):
            service.import_version(
                b,
                _import_request("v1", [_query_case("q2")], parent_version_id=parent.id),
                created_by=None,
            )

    def test_parent_version_in_same_dataset_is_accepted(self, db: Session) -> None:
        service = DatasetService(db)
        dataset_id = _make_dataset(service)
        parent = service.import_version(
            dataset_id, _import_request("v1", [_query_case("q1")]), created_by=None
        )

        child = service.import_version(
            dataset_id,
            _import_request("v2", [_query_case("q2")], parent_version_id=parent.id),
            created_by=None,
        )
        assert child.parent_version_id == parent.id

    def test_unknown_dataset_raises(self, db: Session) -> None:
        service = DatasetService(db)
        with pytest.raises(DatasetNotFoundError):
            service.import_version(10**12, _import_request("v1", [_query_case("q1")]), created_by=None)

    def test_rejected_import_leaves_version_list_unchanged(self, db: Session) -> None:
        """被拒的导入不得留下任何版本行。

        **这条用例证明了什么**：去重校验失败后，版本列表与失败前逐字相同。

        **它没证明什么**：`DuplicateCaseError` 在写库之前就抛出了，所以这里不存在
        「写了一半」的可能，用例并未真正演练回滚。真正会写一半的是
        `_db.flush()` 抛 IntegrityError 的竞态路径——那条路径无法在单进程测试里
        确定性触发（需要两个请求绕过 service 层预检同时提交），其防线是数据库唯一索引，
        由 :meth:`test_content_hash_is_unique_within_version_at_db_level` 与
        `test_schema_constraints.py` 覆盖。
        """
        service = DatasetService(db)
        dataset_id = _make_dataset(service)
        service.import_version(dataset_id, _import_request("v1", [_query_case("q1")]), created_by=None)
        before = [(version.version, count) for version, count in service.list_versions(dataset_id)]

        with pytest.raises(DuplicateCaseError):
            service.import_version(
                dataset_id,
                _import_request("v-bad", [_query_case("q2"), _query_case("q2")]),
                created_by=None,
            )

        after = [(version.version, count) for version, count in service.list_versions(dataset_id)]
        assert after == before == [("v1", 1)]

    def test_case_rows_are_returned_in_insertion_order(self, db: Session) -> None:
        """case 顺序对 NDCG 之类的排序指标有意义，读取必须稳定。"""
        service = DatasetService(db)
        dataset_id = _make_dataset(service)
        version = service.import_version(
            dataset_id,
            _import_request("v1", [_query_case("q1"), _query_case("q2"), _query_case("q3")]),
            created_by=None,
        )

        _, cases = service.get_version(version.id)
        assert [case.payload["query_id"] for case in cases] == ["q1", "q2", "q3"]

    def test_cascade_delete_removes_versions_and_cases(self, db: Session) -> None:
        """删除数据集应连带清掉版本与用例，验证 FK 上的 ON DELETE CASCADE 真的生效。"""
        service = DatasetService(db)
        dataset_id = _make_dataset(service)
        version = service.import_version(dataset_id, _import_request("v1", [_query_case("q1")]), created_by=None)
        version_id = version.id

        db.execute(text("DELETE FROM eval_datasets WHERE id = :id"), {"id": dataset_id})
        db.flush()
        # 必须 expire：db.get() 会先命中 identity map，直接返回缓存中的对象，
        # 即使该行已从库里删掉也看不到——不 expire 这条断言恒假。
        db.expire_all()

        assert db.get(DatasetVersion, version_id) is None
        remaining = db.execute(
            text("SELECT count(*) FROM eval_cases WHERE dataset_version_id = :id"), {"id": version_id}
        ).scalar_one()
        assert remaining == 0
