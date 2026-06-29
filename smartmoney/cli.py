from __future__ import annotations

import asyncio
import logging

import click

from . import db, ingest, report, score, web_report


@click.group()
@click.option("-v", "--verbose", is_flag=True)
def cli(verbose: bool):
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


@cli.command("init-db")
def init_db_cmd():
    """Create the SQLite schema."""
    db.init_db()
    click.echo(f"initialized DB at {db.DB_PATH}")


@cli.command()
@click.option("--wallet", help="ingest a single wallet only")
@click.option("--limit", type=int, default=None, help="max wallets to refresh trades for")
def ingest_cmd(wallet, limit):
    """Pull leaderboard, trades, markets, rebuild positions."""
    if wallet:
        n = ingest.ingest_trades(wallet=wallet)
        click.echo(f"trades for {wallet}: +{n}")
        ingest.refresh_markets()
        ingest.rebuild_positions([wallet])
        return
    stats = ingest.run_daily(limit_wallets=limit)
    for k, v in stats.items():
        click.echo(f"{k}: {v}")


cli.add_command(ingest_cmd, name="ingest")


@cli.command()
def score_cmd():
    """Compute today's scores from trades + price snapshots."""
    n = score.run()
    click.echo(f"scored {n} wallets")


cli.add_command(score_cmd, name="score")


@cli.command()
@click.option("--top-n", type=int, default=None)
def report_cmd(top_n):
    """Render today's Markdown leaderboard."""
    from .config import REPORT_TOP_N
    path = report.generate(top_n=top_n or REPORT_TOP_N)
    click.echo(f"wrote {path}")


cli.add_command(report_cmd, name="report")


@cli.command("export-web")
def export_web_cmd():
    """Dump today's leaderboard to docs/leaderboard.json for the static web UI."""
    path = web_report.export()
    click.echo(f"wrote {path}")


@cli.command()
def archive():
    """Run the WebSocket archiver foreground (use launchd/systemd to daemonize)."""
    from . import archiver
    asyncio.run(archiver.main())


@cli.command("run-daily")
@click.option("--limit", type=int, default=None)
def run_daily(limit):
    """Ingest + score + report — the daily cron entry point."""
    stats = ingest.run_daily(limit_wallets=limit)
    for k, v in stats.items():
        click.echo(f"ingest.{k}: {v}")
    n = score.run()
    click.echo(f"score: {n} wallets")
    path = report.generate()
    click.echo(f"report: {path}")
    web_path = web_report.export()
    click.echo(f"web: {web_path}")


if __name__ == "__main__":
    cli()
