
from pathlib import Path
from rich.console import Console

console = Console()

ASSET_DIR = Path(__file__).parent
MASCOT_FILE = ASSET_DIR / "searchify_mascot.ansi"


def render_mascot():
    """Display the ANSI mascot in the terminal."""

    if not MASCOT_FILE.exists():
        console.print("[bold red]Mascot file not found![/bold red]")
        return

    artwork = MASCOT_FILE.read_text(
        encoding="utf-8",
        errors="replace"
    )

    # Preserve ANSI colour escape sequences
    console.print(
        artwork,
        markup=False,
        highlight=False,
        soft_wrap=True
    )


def show_banner():
    console.clear()

    console.print(
        "\n[bold #F5A8CC]♡ SEARCHIFY ♡[/bold #F5A8CC]",
        justify="center"
    )

    console.print()

    render_mascot()

    console.print(
        "\n[bold #BFA5E8]♫ Your personal AI file assistant ♫[/bold #BFA5E8]",
        justify="center"
    )

    console.print(
        "[bold #67E8F9]● LOCAL AI READY[/bold #67E8F9]",
        justify="center"
    )

    console.print()


if __name__ == "__main__":
    show_banner()
