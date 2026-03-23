"""
ReUpload Detector — точка входа и CLI.
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import click
from loguru import logger
from rich.console import Console
from rich.panel import Panel
from rich.progress import BarColumn, Progress, SpinnerColumn, TaskProgressColumn, TextColumn, TimeRemainingColumn
from rich.prompt import Confirm, Prompt
from rich.table import Table
from rich.tree import Tree
from sqlalchemy import func, select

import config
from core.ai_classifier import gigachat_configured, run_gigachat_on_pending
from core.database import VideoRepository, init_db, session_scope
from core.models import Channel, Video
from core.scheduler import run_monitor_loop, run_scan
from export.excel_export import export_excel
from export.html_export import export_html

console = Console()


def _setup_logging() -> None:
    """Настройка loguru: файл с ротацией и консоль WARNING+."""
    config.ensure_directories()
    log_path = config.LOGS_DIR / "app.log"
    logger.remove()
    logger.add(
        sys.stderr,
        level="WARNING",
        format="{time:YYYY-MM-DD HH:mm:ss} | {level} | {module}:{function}:{line} | {message}",
    )
    logger.add(
        log_path,
        rotation="10 MB",
        retention=5,
        level=config.LOG_LEVEL,
        format="{time:YYYY-MM-DD HH:mm:ss} | {level} | {module}:{function}:{line} | {message}",
        encoding="utf-8",
    )


def _parse_platforms_arg(val: str) -> list[str]:
    parts = [p.strip().lower() for p in val.split(",") if p.strip()]
    out: list[str] = []
    if "both" in parts or ("vk" in parts and "rutube" in parts):
        return ["vk", "rutube"]
    if "vk" in parts:
        out.append("vk")
    if "rutube" in parts:
        out.append("rutube")
    return out or ["vk", "rutube"]


def _interactive_menu() -> None:
    """Интерактивное меню при запуске без подкоманд."""
    console.print(Panel.fit("[bold cyan]🔍 ReUpload Detector v1.0[/bold cyan]", border_style="cyan"))
    keywords = Prompt.ask("Введите ключевые слова через запятую").strip()
    if not keywords:
        console.print("[red]Ключевые слова не могут быть пустыми.[/red]")
        return
    plat = Prompt.ask(
        "Платформы: [1] VK [2] Rutube [3] Обе",
        choices=["1", "2", "3"],
        default="3",
    )
    plat_map = {"1": ["vk"], "2": ["rutube"], "3": ["vk", "rutube"]}
    platforms = plat_map[plat]
    sin = Prompt.ask(
        "Где искать: [1] название [2] описание [3] канал [4] название+описание [5] везде",
        choices=["1", "2", "3", "4", "5"],
        default="5",
    )
    sin_map = {
        "1": "title",
        "2": "description",
        "3": "channel",
        "4": "title+description",
        "5": "all",
    }
    search_in = sin_map[sin]
    max_r = int(Prompt.ask("Макс. результатов на платформу", default=str(config.DEFAULT_MAX_RESULTS)))
    mon = Confirm.ask("Включить режим мониторинга?", default=False)
    interval = config.DEFAULT_CHECK_INTERVAL
    if mon:
        interval = int(Prompt.ask("Интервал проверки (минуты)", default=str(config.DEFAULT_CHECK_INTERVAL)))

    if mon:
        run_monitor_loop(keywords, platforms, search_in, max_r, interval)
        return

    use_gigachat = False
    if gigachat_configured():
        use_gigachat = Confirm.ask(
            "Классифицировать ролики через GigaChat (релевантность проекту «Школа глазами школьника» / Р. Гладенко)?",
            default=False,
        )

    def prog_cb(platform: str, cur: int, tot: int, msg: str) -> None:
        pass

    total_steps = max(1, max_r * max(1, len(platforms)))
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        TimeRemainingColumn(),
        console=console,
    ) as progress:
        task = progress.add_task("Сканирование…", total=total_steps)

        def wrapped(platform: str, cur: int, tot_local: int, msg: str) -> None:
            progress.update(
                task,
                completed=min(cur, total_steps),
                total=total_steps,
                description=msg,
            )

        stats = run_scan(
            keywords,
            platforms,
            search_in,
            max_r,
            progress_cb=wrapped,
            do_export=True,
            run_gigachat=use_gigachat,
        )

    console.print(f"[green]Новых видео: {stats['new_videos']}, новых каналов: {stats['new_channels']}[/green]")
    if stats.get("paths"):
        console.print(f"📄 Excel: {stats['paths'].get('excel', '')}")
        console.print(f"🌐 HTML: {stats['paths'].get('html', '')}")
    _show_results_rich()


@click.group(invoke_without_command=True)
@click.pass_context
def cli(ctx: click.Context) -> None:
    """ReUpload Detector — поиск перезаливов на VK Видео и Rutube."""
    _setup_logging()
    init_db()
    if ctx.invoked_subcommand is None:
        _interactive_menu()


@cli.command("search")
@click.option("--keywords", required=True, help='Ключевые слова через запятую, например: "a, b"')
@click.option(
    "--platforms",
    default="vk,rutube",
    help="vk, rutube или both",
)
@click.option(
    "--search-in",
    "search_in",
    type=click.Choice(["title", "description", "channel", "title+description", "all"]),
    default="all",
)
@click.option("--max-results", default=config.DEFAULT_MAX_RESULTS, type=int)
@click.option("--gigachat", is_flag=True, help="После сканирования классифицировать видео через GigaChat (нужен GIGACHAT_AUTH_KEY)")
def search_cmd(keywords: str, platforms: str, search_in: str, max_results: int, gigachat: bool) -> None:
    """Выполнить одно сканирование и сохранить результаты."""
    pls = _parse_platforms_arg(platforms)

    def prog(platform: str, cur: int, tot: int, msg: str) -> None:
        logger.debug("{} {} {}/{} {}", platform, msg, cur, tot, msg)

    try:
        stats = run_scan(
            keywords,
            pls,
            search_in,
            max_results,
            progress_cb=prog,
            do_export=True,
            run_gigachat=gigachat,
        )
    except Exception as e:
        logger.exception("Ошибка поиска: {}", e)
        console.print(f"[red]Ошибка: {e}[/red]")
        raise SystemExit(1) from e
    console.print(
        Panel(
            f"Совпадений: {stats['total_matched']}\n"
            f"Новых видео: {stats['new_videos']}\n"
            f"Новых каналов: {stats['new_channels']}",
            title="Готово",
        )
    )
    if stats.get("paths"):
        console.print(stats["paths"])


@cli.command("monitor")
@click.option("--keywords", required=True)
@click.option("--interval", default=config.DEFAULT_CHECK_INTERVAL, type=int)
@click.option("--platforms", default="vk,rutube")
@click.option(
    "--search-in",
    "search_in",
    type=click.Choice(["title", "description", "channel", "title+description", "all"]),
    default="all",
)
@click.option("--max-results", default=config.DEFAULT_MAX_RESULTS, type=int)
def monitor_cmd(keywords: str, interval: int, platforms: str, search_in: str, max_results: int) -> None:
    """Режим периодического мониторинга."""
    pls = _parse_platforms_arg(platforms)
    run_monitor_loop(keywords, pls, search_in, max_results, interval)


@cli.command("export")
@click.option("--format", "fmt", type=click.Choice(["excel", "html"]), required=True)
@click.option("--keywords", default="", help="Строка ключевых слов для отчёта (в метаданные)")
def export_cmd(fmt: str, keywords: str) -> None:
    """Экспорт текущей БД в Excel или HTML."""
    init_db()
    kw = keywords or "—"
    with session_scope() as session:
        if fmt == "excel":
            p = export_excel(session, kw)
        else:
            p = export_html(session, kw)
    console.print(f"[green]Сохранено: {p}[/green]")


@cli.command("classify")
@click.option(
    "--reclassify-all",
    is_flag=True,
    help="Переклассифицировать все видео (иначе только без ai_match)",
)
def classify_cmd(reclassify_all: bool) -> None:
    """Запустить классификацию GigaChat для видео в базе."""
    init_db()
    if not gigachat_configured():
        console.print("[red]Укажите GIGACHAT_AUTH_KEY в .env[/red]")
        raise SystemExit(1)
    run_gigachat_on_pending(reclassify_all=reclassify_all, console=console)
    from core.app_settings import load_app_settings

    xf = load_app_settings(1).get("excel_export") or {}
    with session_scope() as session:
        p = export_html(session, "—", export_filters=xf)
        x = export_excel(session, "—", export_filters=xf)
    console.print(f"[green]Отчёты обновлены: {p}, {x}[/green]")


@cli.command("serve")
@click.option("--host", default=None, help="Хост (по умолчанию WEB_API_HOST из .env)")
@click.option("--port", default=None, type=int, help="Порт (по умолчанию WEB_API_PORT из .env)")
def serve_cmd(host: str | None, port: int | None) -> None:
    """Запустить веб-API и (если есть web/dist) раздачу React-панели. Разработка UI: cd web && npm run dev."""
    import uvicorn

    h = host or config.WEB_API_HOST
    p = port if port is not None else config.WEB_API_PORT
    uvicorn.run("web_dashboard:app", host=h, port=p, reload=False)


@cli.command("stats")
def stats_cmd() -> None:
    """Краткая статистика по базе."""
    init_db()
    with session_scope() as session:
        vc = session.scalar(select(func.count()).select_from(Video)) or 0
        cc = session.scalar(select(func.count()).select_from(Channel)) or 0
        by_p = dict(session.execute(select(Video.platform, func.count(Video.id)).group_by(Video.platform)).all())
        ai_yes = session.scalar(select(func.count()).select_from(Video).where(Video.ai_match.is_(True))) or 0
    tbl = Table(title="📊 Статистика БД")
    tbl.add_column("Показатель")
    tbl.add_column("Значение")
    tbl.add_row("Всего видео", str(vc))
    tbl.add_row("Каналов", str(cc))
    tbl.add_row("VK", str(by_p.get("vk", 0)))
    tbl.add_row("Rutube", str(by_p.get("rutube", 0)))
    tbl.add_row("GigaChat: релевантно ШГШ", str(ai_yes))
    console.print(tbl)


def _show_results_rich() -> None:
    """Таблица и дерево топа после интерактивного поиска."""
    top_rows: list[tuple[str, str, int]] = []
    total_v = 0
    with session_scope() as session:
        vid_repo = VideoRepository(session)
        total_v = vid_repo.count()
        top_ch = session.execute(select(Channel).order_by(Channel.total_matching_videos.desc()).limit(5)).scalars().all()
        for ch in top_ch:
            top_rows.append((ch.channel_name, ch.platform, ch.total_matching_videos))
    t = Table(title="Итог")
    t.add_column("Платформа")
    t.add_column("Каналов (топ-5)")
    t.add_column("Видео в БД")
    t.add_row("—", str(len(top_rows)), str(total_v))
    console.print(t)
    tree = Tree("🏆 Топ каналов")
    for i, (name, plat, cnt) in enumerate(top_rows, 1):
        color = "red" if cnt > 10 else "yellow" if cnt >= 5 else "white"
        tree.add(f"[{color}]{i}. {name} ({plat}) — {cnt}[/]")
    console.print(tree)


if __name__ == "__main__":
    cli()
