"""参数快照与模型版本服务（EP-5）。

两张表都是**只读不可变**的（见 `app.params.models` 的模块 docstring），所以这里的
写操作全是「取或建」（get-or-create）语义，不是 upsert：找到同哈希的行就复用，
绝不更新已有行。理由很实在——历史 run 通过外键回指这些行，改一行就会让
「某次指标是在什么配置下算出来的」这个问题的答案被悄悄篡改。

并发安全依赖两处配合：应用层先查、数据库唯一约束兜底。两个请求同时插入同哈希时，
后者会撞唯一键，此时**重读并复用**而不是报错——对调用方而言语义仍是「取或建」。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.connector import JavaEvalClient
from app.connector.schemas import ParamsResponse
from app.datasets.models import DatasetVersion
from app.datasets.service import VersionNotFoundError
from app.params.fingerprint import (
    DEFAULT_FROZEN_KEYS,
    config_fingerprint,
    model_config_hash,
    params_hash,
)
from app.params.models import ModelVersion, ParamSnapshot
from app.settings.logging import get_logger

logger = get_logger(__name__)


class ParamSnapshotNotFoundError(Exception):
    pass


class ModelVersionNotFoundError(Exception):
    pass


class ParamService:
    """参数快照、模型版本与指纹组合。"""

    def __init__(self, db: Session) -> None:
        self._db = db

    # -- 参数快照 ---------------------------------------------------------

    def get_or_create_param_snapshot(
        self,
        *,
        name: str,
        params: dict[str, Any],
        frozen_keys: list[str] | None = None,
        description: str | None = None,
        created_by: int | None = None,
    ) -> tuple[ParamSnapshot, bool]:
        """按 ``params_hash`` 取或建。返回 ``(快照, 是否新建)``。

        ``frozen_keys`` 原样存进 ``freeze_config``——审计时要能回答
        「这次指纹到底冻结了哪些字段」，只存哈希是不够的。
        """
        keys = sorted(frozen_keys) if frozen_keys is not None else sorted(DEFAULT_FROZEN_KEYS)
        digest = params_hash(params, keys)

        existing = self._find_snapshot_by_hash(digest)
        if existing is not None:
            return existing, False

        snapshot = ParamSnapshot(
            name=name,
            params=params,
            freeze_config={"frozen_keys": keys},
            params_hash=digest,
            description=description,
            created_by=created_by,
        )
        self._db.add(snapshot)
        try:
            self._db.flush()
        except IntegrityError:
            # 并发插入同哈希：唯一约束挡住了对方先到的行，重读复用即可。
            # 必须先回滚这个失败的事务，否则后续查询在同事务里不可用。
            self._db.rollback()
            raced = self._find_snapshot_by_hash(digest)
            if raced is None:
                raise
            logger.info("参数快照并发撞键，复用已有行 id=%s", raced.id)
            return raced, False

        logger.info("创建参数快照 id=%s hash=%s", snapshot.id, digest)
        return snapshot, True

    def _find_snapshot_by_hash(self, digest: str) -> ParamSnapshot | None:
        return self._db.execute(
            select(ParamSnapshot).where(ParamSnapshot.params_hash == digest)
        ).scalar_one_or_none()

    def get_param_snapshot(self, snapshot_id: int) -> ParamSnapshot:
        snapshot = self._db.get(ParamSnapshot, snapshot_id)
        if snapshot is None:
            raise ParamSnapshotNotFoundError(f"参数快照不存在: id={snapshot_id}")
        return snapshot

    def list_param_snapshots(self) -> list[ParamSnapshot]:
        return list(self._db.execute(select(ParamSnapshot).order_by(ParamSnapshot.id)).scalars())

    def fetch_params_from_java(self, client: JavaEvalClient) -> dict[str, Any]:
        """从 AgentWrite ``/api/v1/eval/params`` 拉取参数原文。

        刻意**不做字段改名或裁剪**：拉回来什么就存什么，冻结范围由 ``frozen_keys``
        决定。这样 Java 侧新增参数时快照会自动带上，不会因为平台"只认识旧字段"而丢信息。
        """
        response: ParamsResponse = client.params()
        return response.model_dump()

    # -- 模型版本 ---------------------------------------------------------

    def get_or_create_model_version(
        self,
        *,
        embedding_model_id: str,
        reranker_model_id: str,
        config: dict[str, Any] | None = None,
    ) -> tuple[ModelVersion, bool]:
        """按 ``config_hash`` 取或建。"""
        config = config or {}
        digest = model_config_hash(embedding_model_id, reranker_model_id, config)

        existing = self._find_model_version_by_hash(digest)
        if existing is not None:
            return existing, False

        version = ModelVersion(
            embedding_model_id=embedding_model_id,
            reranker_model_id=reranker_model_id,
            config_hash=digest,
            config=config,
        )
        self._db.add(version)
        try:
            self._db.flush()
        except IntegrityError:
            self._db.rollback()
            raced = self._find_model_version_by_hash(digest)
            if raced is None:
                raise
            logger.info("模型版本并发撞键，复用已有行 id=%s", raced.id)
            return raced, False

        logger.info("创建模型版本 id=%s hash=%s", version.id, digest)
        return version, True

    def _find_model_version_by_hash(self, digest: str) -> ModelVersion | None:
        return self._db.execute(
            select(ModelVersion).where(ModelVersion.config_hash == digest)
        ).scalar_one_or_none()

    def get_model_version(self, version_id: int) -> ModelVersion:
        version = self._db.get(ModelVersion, version_id)
        if version is None:
            raise ModelVersionNotFoundError(f"模型版本不存在: id={version_id}")
        return version

    def list_model_versions(self) -> list[ModelVersion]:
        return list(self._db.execute(select(ModelVersion).order_by(ModelVersion.id)).scalars())

    # -- fingerprint 组合 -------------------------------------------------

    def compose_fingerprint(
        self,
        *,
        param_snapshot_id: int,
        model_version_id: int,
        dataset_version_id: int,
        mode: str,
        case_limit: int | None = None,
    ) -> dict[str, str]:
        """把库中三份不可变配置的哈希、mode 与 case_limit 组合成 ``config_fingerprint``。

        **从库里读哈希，而不是从请求里重算**：请求携带的 params 可能与已落库的快照
        不一致（客户端算错、或快照被换过），那样算出的指纹会指向一个实际不存在于
        库中的配置组合。以库为准才能保证「同一指纹 → 同一配置」这个反查成立。

        ``case_limit`` 是唯一的例外——它不是库里的配置，而是本次 run 的取数范围。
        参与指纹的理由见 :func:`app.params.fingerprint.config_fingerprint`。
        """
        snapshot = self.get_param_snapshot(param_snapshot_id)
        model_version = self.get_model_version(model_version_id)
        dataset_version = self._db.get(DatasetVersion, dataset_version_id)
        if dataset_version is None:
            raise VersionNotFoundError(f"数据集版本不存在: id={dataset_version_id}")

        fingerprint = config_fingerprint(
            params_digest=snapshot.params_hash,
            model_digest=model_version.config_hash,
            dataset_digest=dataset_version.content_digest,
            mode=mode,
            case_limit=case_limit,
        )
        return {
            "config_fingerprint": fingerprint,
            "params_hash": snapshot.params_hash,
            "model_config_hash": model_version.config_hash,
            "dataset_content_digest": dataset_version.content_digest,
            "mode": mode,
        }
