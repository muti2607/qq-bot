"""调度器：把每个任务注册成 APScheduler 的 cron 任务。"""
from __future__ import annotations

import datetime as _dt
import logging
import threading

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

import db
import onebot
import sender

log = logging.getLogger("scheduler")

_scheduler: BackgroundScheduler | None = None
_send_lock = threading.Lock()  # 串行发送，避免机器人和 NapCat 同时被打爆


def start() -> None:
    global _scheduler
    if _scheduler is not None:
        return
    _scheduler = BackgroundScheduler(
        job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 600},
        timezone="Asia/Shanghai",
    )
    _scheduler.start()
    reload_all()
    log.info("调度器已启动")
    try:
        db.prune_old_logs(30)
    except Exception as e:  # 清理失败不影响启动
        log.warning("清理旧日志失败: %s", e)


def shutdown() -> None:
    global _scheduler
    if _scheduler:
        _scheduler.shutdown(wait=False)
        _scheduler = None


def _add_job(task: dict) -> None:
    if not _scheduler:
        return
    job_id = f"task:{task['id']}"
    if not task.get("enabled"):
        _remove(job_id)
        return
    try:
        trigger = CronTrigger.from_crontab(task["schedule"].strip(), timezone=sender.tz_of(task))
    except Exception as e:
        db.add_log(f"任务《{task['name'] or task['id']}》调度失败（{task['schedule']}）：{e}", "error", task["id"])
        _remove(job_id)
        return
    try:
        _scheduler.add_job(
            run_task,
            trigger=trigger,
            id=job_id,
            args=[task["id"]],
            replace_existing=True,
        )
    except Exception as e:
        db.add_log(f"任务《{task['name'] or task['id']}》调度失败：{e}", "error", task["id"])
        _remove(job_id)


def _remove(job_id: str) -> None:
    if _scheduler:
        try:
            _scheduler.remove_job(job_id)
        except Exception:
            pass


def reload_all() -> None:
    if not _scheduler:
        return
    for job in _scheduler.get_jobs():
        if job.id.startswith("task:"):
            _remove(job.id)
    for task in db.list_tasks():
        _add_job(task)


def reload_task(task_id: int) -> None:
    task = db.get_task(task_id)
    if not task:
        _remove(f"task:{task_id}")
        return
    _add_job(task)


def next_runs() -> dict[int, str]:
    if not _scheduler:
        return {}
    out: dict[int, str] = {}
    for job in _scheduler.get_jobs():
        if job.id.startswith("task:") and job.next_run_time:
            try:
                out[int(job.id.split(":")[1])] = job.next_run_time.strftime("%Y-%m-%d %H:%M")
            except ValueError:
                pass
    return out


def run_task(task_id: int, override_text: str | None = None, targets_override: list | None = None) -> dict:
    """执行一次任务。返回 {ok, sent, failed, errors}"""
    task = db.get_task(task_id)
    if not task:
        return {"ok": False, "sent": 0, "failed": 0, "errors": ["任务不存在"]}

    now = _dt.datetime.now(sender.tz_of(task))
    label = task["name"] or f"#{task['id']}"
    targets = targets_override if targets_override is not None else (task.get("targets") or [])
    if override_text is not None:
        task = dict(task)
        task["content"] = override_text
        task["source_mode"] = "static"

    if not targets:
        db.add_log(f"任务《{label}》没有设置发送目标，跳过", "warn", task_id)
        return {"ok": False, "sent": 0, "failed": 0, "errors": ["没有发送目标"]}

    try:
        segments = sender.build_segments(task, now)
    except Exception as e:
        db.add_log(f"任务《{label}》内容生成失败：{e}", "error", task_id)
        return {"ok": False, "sent": 0, "failed": 0, "errors": [str(e)]}

    sent = failed = 0
    errors: list[str] = []
    with _send_lock:
        for t in targets:
            ttype = t.get("type")
            tid = t.get("id")
            tname = t.get("name") or str(tid)
            if ttype not in ("group", "private") or not tid:
                errors.append(f"目标格式错误：{t}")
                failed += 1
                continue
            try:
                onebot.send_message(ttype, tid, segments, at_all=bool(task.get("at_all")) and ttype == "group")
                sent += 1
                db.add_log(f"任务《{label}》已发送到 {'群' if ttype == 'group' else '私聊'} {tname}({tid})", "info", task_id)
            except Exception as e:
                failed += 1
                errors.append(f"{tname}: {e}")
                db.add_log(f"任务《{label}》发送到 {tname}({tid}) 失败：{e}", "error", task_id)

    return {"ok": failed == 0, "sent": sent, "failed": failed, "errors": errors}
