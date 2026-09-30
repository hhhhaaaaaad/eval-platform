"""Celery 任务注册的回归测试。

**为什么单列一个文件**：这类缺陷在常规测试里完全不可见——测试直接调函数，
不经过 worker 的任务注册表。只有真起一个 worker 才会看到
``Received unregistered task ... has been ignored and discarded``。

这个 bug 的实际形态（已踩过一次）：worker 以 ``-A app.tasks.celery_app.celery_app``
启动时只导入 ``celery_app`` 模块本身，其他任务模块里的装饰器从未执行，
任务因此未注册。而 API 侧 ``delay()`` 投递成功仍返回 True，
于是 run 永远停在 pending，且兜底的 reaper 也没注册——**没有任何进程会来收拾**。

这里的做法是**模拟 worker 启动时的模块加载动作**（``import_default_modules``），
再断言任务确实在注册表里。这样不必真起 worker 也能守住。
"""

from __future__ import annotations

import importlib
import pkgutil
from pathlib import Path

import pytest

from app.tasks import celery_app as celery_app_module
from app.tasks.celery_app import celery_app

TASKS_PACKAGE = Path(celery_app_module.__file__).parent

#: 期望注册的任务名。新增任务时在此登记，否则下面的断言不会覆盖到它。
EXPECTED_TASKS = (
    "app.tasks.eval_tasks.execute_run",
    "app.tasks.eval_tasks.reap_stale_runs",
    # pending 补投：与 reaper 同为兜底任务，漏注册的后果同样是「没有任何进程会来收拾」。
    "app.tasks.eval_tasks.dispatch_pending_runs",
    "app.tasks.celery_app.ping",
)


@pytest.fixture(scope="module")
def registered() -> set[str]:
    """模拟 worker 启动时的模块加载，返回已注册的任务名集合。

    ``import_default_modules`` 正是 Celery worker 启动时用来加载
    ``include`` / ``imports`` 里那些模块的动作——用它就等价于
    「worker 起来后认得哪些任务」。
    """
    celery_app.loader.import_default_modules()
    return set(celery_app.tasks)


class TestTaskRegistration:
    @pytest.mark.parametrize("task_name", EXPECTED_TASKS)
    def test_expected_tasks_are_registered(self, registered: set[str], task_name: str) -> None:
        assert task_name in registered, (
            f"任务 {task_name} 未注册——worker 会丢弃它。"
            "检查 app/tasks/celery_app.py 的 include 列表是否登记了所属模块。"
        )

    def test_every_task_module_is_included(self) -> None:
        """``app/tasks/`` 下每个含任务定义的模块都必须在 ``include`` 里。

        这条是防「新增任务模块忘了登记」的守卫：漏登记的后果不是报错，
        而是任务被静默丢弃。``ping`` 定义在 ``celery_app`` 自身，故排除。
        """
        include = set(celery_app.conf.include or [])
        missing: list[str] = []

        for module_info in pkgutil.iter_modules([str(TASKS_PACKAGE)]):
            if module_info.name in ("celery_app", "__init__"):
                continue  # 定义在 celery_app 自身的任务随主模块一起加载
            module = importlib.import_module(f"app.tasks.{module_info.name}")
            source = Path(module.__file__ or "").read_text(encoding="utf-8")
            defines_task = "@celery_app.task" in source or "@shared_task" in source
            if defines_task and f"app.tasks.{module_info.name}" not in include:
                missing.append(module_info.name)

        assert not missing, (
            f"以下模块定义了 Celery 任务但未登记进 include：{missing}。"
            "未登记会让 worker 丢弃任务且不报错。"
        )

    def test_beat_schedule_tasks_are_registered(self, registered: set[str]) -> None:
        """beat 调度里引用的任务名必须真实存在。

        写错任务名的后果同样静默：beat 按周期触发一个不存在的任务，
        worker 每次丢弃，reaper 实际上从未运行过——而日志里只有一行
        「unregistered task」，很容易被当成噪声忽略。
        """
        schedule = celery_app.conf.beat_schedule or {}
        assert schedule, "beat_schedule 为空，reaper 将永不运行"

        for name, entry in schedule.items():
            task_name = entry["task"]
            assert task_name in registered, f"beat 条目 {name} 引用了未注册的任务 {task_name}"

    def test_beat_schedule_interval_is_positive(self) -> None:
        for name, entry in (celery_app.conf.beat_schedule or {}).items():
            assert float(entry["schedule"]) > 0, f"beat 条目 {name} 的周期非正数"


class TestOwnerName:
    """租约持有者标识必须是纯 ASCII。

    它会作为 ``X-Eval-Run-Id`` 请求头发出去，而 HTTP 头值按规范是 ASCII——
    httpx 会 ``value.encode("ascii")``，非 ASCII 直接抛 UnicodeEncodeError。
    中文 Windows 机器名（国内极常见）会让 worker 一跑任务就崩，
    且崩在**错误处理路径**上，连失败记录都写不下去。
    """

    def test_actual_host_name_is_ascii(self) -> None:
        from app.tasks.eval_tasks import _owner_name

        _owner_name().encode("ascii")  # 不抛即通过

    def test_non_ascii_host_falls_back_to_hash(self, monkeypatch) -> None:
        import socket

        from app.tasks import eval_tasks

        monkeypatch.setattr(socket, "gethostname", lambda: "某某的电脑")
        owner = eval_tasks._owner_name()

        owner.encode("ascii")
        assert owner.startswith("host-")
        assert ":" in owner  # 仍带 pid

    def test_owner_is_usable_as_http_header(self, monkeypatch) -> None:
        """精确复现当初的崩溃点：把 owner 放进请求头。

        只断言「字符串是 ASCII」还不够——真正的失败发生在 httpx 构造 Headers 时，
        所以这里直接用 httpx 构造一次。
        """
        import socket

        import httpx

        from app.tasks import eval_tasks

        monkeypatch.setattr(socket, "gethostname", lambda: "某某的电脑")
        headers = httpx.Headers({"X-Eval-Run-Id": eval_tasks._owner_name()})
        assert headers["X-Eval-Run-Id"]

    def test_owner_contains_pid_for_disambiguation(self) -> None:
        import os

        from app.tasks.eval_tasks import _owner_name

        assert _owner_name().endswith(f":{os.getpid()}")


class TestDispatchReferences:
    def test_dispatch_references_registered_task(self, registered: set[str]) -> None:
        """投递逻辑引用的任务对象必须与注册表里的名字一致。

        如果 ``dispatch`` 用了一个改名后的任务（或新任务写了新的 ``name=``），
        API 会照常返回 ``enqueued=True``，而 worker 侧照样丢弃。
        """
        from app.tasks.eval_tasks import execute_run

        assert execute_run.name in registered
        assert execute_run.name == "app.tasks.eval_tasks.execute_run"

    def test_reaper_task_name_matches_beat_schedule(self) -> None:
        from app.tasks.eval_tasks import reap_stale_runs

        beat = (celery_app.conf.beat_schedule or {}).get("reap-stale-runs")
        assert beat is not None, "beat_schedule 缺少 reap-stale-runs 条目"
        assert beat["task"] == reap_stale_runs.name
