import json
import logging
import os
import sys
from typing import Dict, List, Optional

from localstack import config
from localstack.utils.analytics.cli import publish_invocation

if sys.version_info >= (3, 8):
    from typing import TypedDict
else:
    from typing_extensions import TypedDict

import click

from localstack import __version__

from .console import BANNER, console
from .plugin import LocalstackCli, load_cli_plugins


def create_with_plugins() -> LocalstackCli:
    """
    Creates a LocalstackCli instance with all cli plugins loaded.
    :return: a LocalstackCli instance
    """
    cli = LocalstackCli()
    cli.group = localstack
    load_cli_plugins(cli)
    return cli


def _setup_cli_debug():
    from localstack.logging.setup import setup_logging_for_cli

    config.DEBUG = True
    os.environ["DEBUG"] = "1"

    setup_logging_for_cli(logging.DEBUG if config.DEBUG else logging.INFO)


@click.group(name="localstack", help="The LocalStack Command Line Interface (CLI)")
@click.version_option(version=__version__, message="%(version)s")
@click.option("--debug", is_flag=True, help="Enable CLI debugging mode")
@click.option("--profile", type=str, help="Set the configuration profile")
def localstack(debug, profile):
    # --profile is read manually in localstack.cli.main because it needs to be read before localstack.config is read

    if debug:
        _setup_cli_debug()

    from localstack.utils.files import cache_dir

    # overwrite the config variable here to defer import of cache_dir
    if not os.environ.get("LOCALSTACK_VOLUME_DIR", "").strip():
        config.VOLUME_DIR = str(cache_dir() / "volume")

    # FIXME: at some point we should remove the use of `config.dirs` for the CLI,
    #  see https://github.com/localstack/localstack/pull/7906
    config.dirs.for_cli().mkdirs()


@localstack.group(name="config", help="Inspect your LocalStack configuration")
def localstack_config():
    pass


@localstack.group(
    name="status",
    help="Print status information about the LocalStack runtime",
    invoke_without_command=True,
)
@click.pass_context
def localstack_status(ctx):
    if ctx.invoked_subcommand is None:
        ctx.invoke(localstack_status.get_command(ctx, "docker"))


@localstack_status.command(
    name="docker", help="Query information about the LocalStack Docker image and runtime"
)
@click.option("--format", type=click.Choice(["table", "plain", "dict", "json"]), default="table")
def cmd_status_docker(format):
    with console.status("Querying Docker status"):
        print_docker_status(format)


@localstack_status.command(name="services", help="Query information about running services")
@click.option("--format", type=click.Choice(["table", "plain", "dict", "json"]), default="table")
def cmd_status_services(format):
    import requests

    url = config.get_edge_url()

    try:
        health = requests.get(f"{url}/_localstack/health", timeout=2)
        doc = health.json()
        services = doc.get("services", [])
        if format == "table":
            print_service_table(services)
        if format == "plain":
            for service, status in services.items():
                console.print(f"{service}={status}")
        if format == "dict":
            console.print(services)
        if format == "json":
            console.print(json.dumps(services))
    except requests.ConnectionError:
        error = f"could not connect to LocalStack health endpoint at {url}"
        print_error(format, error)
        if config.DEBUG:
            console.print_exception()
        sys.exit(1)


@localstack.command(name="start", help="Start LocalStack")
@click.option("--docker", is_flag=True, help="Start LocalStack in a docker container (default)")
@click.option("--host", is_flag=True, help="Start LocalStack directly on the host")
@click.option("--no-banner", is_flag=True, help="Disable LocalStack banner", default=False)
@click.option(
    "-d", "--detached", is_flag=True, help="Start LocalStack in the background", default=False
)
@publish_invocation
def cmd_start(docker: bool, host: bool, no_banner: bool, detached: bool):
    if docker and host:
        raise click.ClickException("Please specify either --docker or --host")
    if host and detached:
        raise click.ClickException("Cannot start detached in host mode")

    if not no_banner:
        print_banner()
        print_version()
        print_profile()
        console.line()

    from localstack.utils import bootstrap

    if not no_banner:
        if host:
            console.log("starting LocalStack in host mode :laptop_computer:")
        else:
            console.log("starting LocalStack in Docker mode :whale:")

    bootstrap.prepare_host(console)

    if not no_banner and not detached:
        console.rule("LocalStack Runtime Log (press [bold][yellow]CTRL-C[/yellow][/bold] to quit)")

    if host:
        # from here we abandon the regular CLI control path and start treating the process like a localstack
        # runtime process
        os.environ["LOCALSTACK_CLI"] = "0"
        config.dirs = config.init_directories()

        try:
            bootstrap.start_infra_locally()
        except ImportError:
            if config.DEBUG:
                console.print_exception()
            raise click.ClickException(
                "It appears you have a light install of localstack which only supports running in docker.\n"
                "If you would like to use --host, please install localstack with Python using "
                "`pip install localstack[runtime]` instead."
            )
    else:
        # make sure to initialize the bootstrap environment and directories for the host (even if we're executing
        # in Docker), to allow starting the container from within other containers (e.g., Github Codespaces).
        config.OVERRIDE_IN_DOCKER = False
        config.is_in_docker = False
        config.dirs = config.init_directories()

        if detached:
            bootstrap.start_infra_in_docker_detached(console)
        else:
            bootstrap.start_infra_in_docker()


@localstack.command(name="stop", help="Stop the running LocalStack container")
@publish_invocation
def cmd_stop():
    from localstack.utils.docker_utils import DOCKER_CLIENT

    from ..utils.container_utils.container_client import NoSuchContainer

    container_name = config.MAIN_CONTAINER_NAME

    try:
        DOCKER_CLIENT.stop_container(container_name)
        console.print("container stopped: %s" % container_name)
    except NoSuchContainer:
        console.print("no such container: %s" % container_name)
        sys.exit(1)


@localstack.command(name="logs", help="Show the logs of the LocalStack container")
@click.option(
    "-f",
    "--follow",
    is_flag=True,
    help="Block the terminal and follow the log output",
    default=False,
)
@click.option(
    "-n",
    "--tail",
    type=int,
    help="Print only the last N lines of the log output",
    default=None,
)
@publish_invocation
def cmd_logs(follow: bool, tail: int):
    from localstack.utils.bootstrap import LocalstackContainer
    from localstack.utils.docker_utils import DOCKER_CLIENT

    container_name = config.MAIN_CONTAINER_NAME
    logfile = LocalstackContainer(container_name).logfile

    if not DOCKER_CLIENT.is_container_running(container_name):
        console.print("localstack container not running")
        if os.path.exists(logfile):
            console.print("printing logs from previous run")
            with open(logfile) as fd:
                for line in fd:
                    click.echo(line, nl=False)
        sys.exit(1)

    if follow:
        num_lines = 0
        for line in DOCKER_CLIENT.stream_container_logs(container_name):
            print(line.decode("utf-8").rstrip("\r\n"))
            num_lines += 1
            if tail is not None and num_lines >= tail:
                break

    else:
        logs = DOCKER_CLIENT.get_container_logs(container_name)
        if tail is not None:
            logs = "\n".join(logs.split("\n")[-tail:])
        print(logs)


@localstack.command(name="wait", help="Wait on the LocalStack container to start")
@click.option(
    "-t",
    "--timeout",
    type=float,
    help="The amount of time in seconds to wait before raising a timeout error",
    default=None,
)
@publish_invocation
def cmd_wait(timeout: Optional[float] = None):
    from localstack.utils.bootstrap import wait_container_is_ready

    if not wait_container_is_ready(timeout=timeout):
        raise click.ClickException("timeout")


@localstack_config.command(
    name="validate", help="Validate your LocalStack configuration (e.g., your docker-compose.yml)"
)
@click.option(
    "--file",
    default="docker-compose.yml",
    type=click.Path(exists=True, file_okay=True, readable=True),
)
@publish_invocation
def cmd_config_validate(file):
    from rich.panel import Panel

    from localstack.utils import bootstrap

    try:
        if bootstrap.validate_localstack_config(file):
            console.print("[green]:heavy_check_mark:[/green] config valid")
            sys.exit(0)
        else:
            console.print("[red]:heavy_multiplication_x:[/red] validation error")
            sys.exit(1)
    except Exception as e:
        console.print(Panel(str(e), title="[red]Error[/red]", expand=False))
        console.print("[red]:heavy_multiplication_x:[/red] validation error")
        sys.exit(1)


@localstack_config.command(name="show", help="Print the current LocalStack config values")
@click.option("--format", type=click.Choice(["table", "plain", "dict", "json"]), default="table")
@publish_invocation
def cmd_config_show(format):
    # TODO: parse values from potential docker-compose file?
    assert config

    try:
        # only load the ext config if it's available
        from localstack_ext import config as ext_config

        assert ext_config
    except ImportError:
        # the ext package is not available
        return None

    if format == "table":
        print_config_table()
    elif format == "plain":
        print_config_pairs()
    elif format == "dict":
        print_config_dict()
    elif format == "json":
        print_config_json()
    else:
        print_config_pairs()  # fall back to plain


def print_config_json():
    import json

    console.print(json.dumps(dict(config.collect_config_items())))


def print_config_pairs():
    for key, value in config.collect_config_items():
        console.print(f"{key}={value}")


def print_config_dict():
    console.print(dict(config.collect_config_items()))


def print_config_table():
    from rich.table import Table

    grid = Table(show_header=True)
    grid.add_column("Key")
    grid.add_column("Value")

    for key, value in config.collect_config_items():
        grid.add_row(key, str(value))

    console.print(grid)


@localstack.command(name="ssh", help="Obtain a shell in the running LocalStack container")
@publish_invocation
def cmd_ssh():
    from localstack.utils.docker_utils import DOCKER_CLIENT
    from localstack.utils.run import run

    if not DOCKER_CLIENT.is_container_running(config.MAIN_CONTAINER_NAME):
        raise click.ClickException(
            'Expected a running container named "%s", but found none' % config.MAIN_CONTAINER_NAME
        )
    try:
        process = run("docker exec -it %s bash" % config.MAIN_CONTAINER_NAME, tty=True)
        process.wait()
    except KeyboardInterrupt:
        pass


@localstack.group(name="update", help="Update LocalStack components")
def localstack_update():
    pass


@localstack_update.command(name="all", help="Update all LocalStack components")
@click.pass_context
@publish_invocation
def cmd_update_all(ctx):
    ctx.invoke(localstack_update.get_command(ctx, "localstack-cli"))
    ctx.invoke(localstack_update.get_command(ctx, "docker-images"))


@localstack_update.command(name="localstack-cli", help="Update LocalStack CLI tools")
@publish_invocation
def cmd_update_localstack_cli():
    if is_frozen_bundle():
        # "update" can only be performed if running from source / in a non-frozen interpreter
        console.print(
            ":heavy_multiplication_x: The LocalStack CLI can only update itself if installed via PIP. "
            "Please follow the instructions on https://docs.localstack.cloud/ to update your CLI.",
            style="bold red",
        )
        sys.exit(1)

    import subprocess
    from subprocess import CalledProcessError

    console.rule("Updating LocalStack CLI")
    with console.status("Updating LocalStack CLI..."):
        try:
            subprocess.check_output(
                [sys.executable, "-m", "pip", "install", "--upgrade", "localstack"]
            )
            console.print(":heavy_check_mark: LocalStack CLI updated")
        except CalledProcessError:
            console.print(":heavy_multiplication_x: LocalStack CLI update failed", style="bold red")


@localstack_update.command(
    name="docker-images", help="Update container images LocalStack depends on"
)
@publish_invocation
def cmd_update_docker_images():
    from localstack.utils.docker_utils import DOCKER_CLIENT

    console.rule("Updating docker images")

    all_images = DOCKER_CLIENT.get_docker_image_names(strip_latest=False)
    image_prefixes = [
        "localstack/",
        "lambci/lambda:",
        "mlupin/docker-lambda:",
        "public.ecr.aws/lambda",
    ]
    localstack_images = [
        image
        for image in all_images
        if any(
            image.startswith(image_prefix) or image.startswith(f"docker.io/{image_prefix}")
            for image_prefix in image_prefixes
        )
        and not image.endswith(":<none>")  # ignore dangling images
    ]
    update_images(localstack_images)


def update_images(image_list: List[str]):
    from rich.markup import escape
    from rich.progress import MofNCompleteColumn, Progress

    from localstack.utils.container_utils.container_client import ContainerException
    from localstack.utils.docker_utils import DOCKER_CLIENT

    updated_count = 0
    failed_count = 0
    progress = Progress(
        *Progress.get_default_columns(), MofNCompleteColumn(), transient=True, console=console
    )
    with progress:
        for image in progress.track(image_list, description="Processing image..."):
            try:
                updated = False
                hash_before_pull = DOCKER_CLIENT.inspect_image(image_name=image, pull=False)["Id"]
                DOCKER_CLIENT.pull_image(image)
                if (
                    hash_before_pull
                    != DOCKER_CLIENT.inspect_image(image_name=image, pull=False)["Id"]
                ):
                    updated = True
                    updated_count += 1
                console.print(
                    f":heavy_check_mark: Image {escape(image)} {'updated' if updated else 'up-to-date'}.",
                    style="bold" if updated else None,
                    highlight=False,
                )
            except ContainerException as e:
                console.print(
                    f":heavy_multiplication_x: Image {escape(image)} pull failed: {e.message}",
                    style="bold red",
                    highlight=False,
                )
                failed_count += 1
    console.rule()
    console.print(
        f"Images updated: {updated_count}, Images failed: {failed_count}, total images processed: {len(image_list)}."
    )


# legacy support
@localstack.group(name="infra", help="Manipulate LocalStack infrastructure", deprecated=True)
def infra():
    pass


@infra.command("start")
@click.pass_context
@click.option("--docker", is_flag=True, help="Start LocalStack in a docker container (default)")
@click.option("--host", is_flag=True, help="Start LocalStack directly on the host")
@publish_invocation
def cmd_infra_start(ctx, *args, **kwargs):
    ctx.invoke(cmd_start, *args, **kwargs)


@localstack.command(
    name="completion",
    short_help="CLI shell completion",
    help="""
         Print shell completion code for the specified shell (bash, zsh, or fish).
         The shell code must be evaluated to enable the interactive shell completion of LocalStack CLI commands.
         This is usually done by sourcing it from the .bash_profile.

         \b
         Examples:
           # Bash
           ## Bash completion on Linux depends on the 'bash-completion' package.
           ## Write the LocalStack CLI completion code for bash to a file and source it from .bash_profile
           localstack completion bash > ~/.localstack/completion.bash.inc
           printf "
           # LocalStack CLI bash completion
           source '$HOME/.localstack/completion.bash.inc'
           " >> $HOME/.bash_profile
           source $HOME/.bash_profile
        \b
           # zsh
           ## Set the LocalStack completion code for zsh to autoload on startup:
           localstack completion zsh > "${fpath[1]}/_localstack"
        \b
           # fish
           ## Set the LocalStack completion code for fish to autoload on startup:
           localstack completion fish > ~/.config/fish/completions/localstack.fish
         """,
)
@click.pass_context
@click.argument(
    "shell", required=True, type=click.Choice(["bash", "zsh", "fish"], case_sensitive=False)
)
@publish_invocation
def localstack_completion(ctx: click.Context, shell: str):
    """
    Click's autocompletion is not perfectly user-friendly.
    This command prints the autocompletion script of Click for this program.
    It can then be simply piped to a completion file.
    Example:
        localstack complete fish > ~/.config/fish/completions/localstack.fish
    :param ctx: Click context
    :param shell: Shell to show the autocompletion script for
    """
    # lookup the completion, raise an error if the given completion is not found
    import click.shell_completion

    comp_cls = click.shell_completion.get_completion_class(shell)
    if comp_cls is None:
        raise click.ClickException("Completion for given shell could not be found.")

    # Click's program name is the base path of sys.argv[0]
    path = sys.argv[0]
    prog_name = os.path.basename(path)

    # create the completion variable according to the docs
    # https://click.palletsprojects.com/en/8.1.x/shell-completion/#enabling-completion
    complete_var = f"_{prog_name}_COMPLETE".replace("-", "_").upper()

    # instantiate the completion class and print the completion source
    comp = comp_cls(ctx.command, {}, prog_name, complete_var)
    click.echo(comp.source())


class DockerStatus(TypedDict, total=False):
    running: bool
    runtime_version: str
    image_tag: str
    image_id: str
    image_created: str
    container_name: Optional[str]
    container_ip: Optional[str]


def print_docker_status(format):
    from localstack.utils import docker_utils
    from localstack.utils.bootstrap import get_docker_image_details, get_server_version
    from localstack.utils.container_networking import get_main_container_ip, get_main_container_name

    img = get_docker_image_details()
    cont_name = config.MAIN_CONTAINER_NAME
    running = docker_utils.DOCKER_CLIENT.is_container_running(cont_name)
    status = DockerStatus(
        runtime_version=get_server_version(),
        image_tag=img["tag"],
        image_id=img["id"],
        image_created=img["created"],
        running=running,
    )
    if running:
        status["container_name"] = get_main_container_name()
        status["container_ip"] = get_main_container_ip()

    if format == "dict":
        console.print(status)
    if format == "table":
        print_docker_status_table(status)
    if format == "json":
        console.print(json.dumps(status))
    if format == "plain":
        for key, value in status.items():
            console.print(f"{key}={value}")


def print_docker_status_table(status: DockerStatus):
    from rich.table import Table

    grid = Table(show_header=False)
    grid.add_column()
    grid.add_column()

    grid.add_row("Runtime version", f'[bold]{status["runtime_version"]}[/bold]')
    grid.add_row(
        "Docker image",
        f"tag: {status['image_tag']}, "
        f"id: {status['image_id']}, "
        f":calendar: {status['image_created']}",
    )
    cont_status = "[bold][red]:heavy_multiplication_x: stopped"
    if status["running"]:
        cont_status = (
            f"[bold][green]:heavy_check_mark: running[/green][/bold] "
            f'(name: "[italic]{status["container_name"]}[/italic]", IP: {status["container_ip"]})'
        )
    grid.add_row("Runtime status", cont_status)
    console.print(grid)


def print_service_table(services: Dict[str, str]):
    from rich.table import Table

    status_display = {
        "running": "[green]:heavy_check_mark:[/green] running",
        "starting": ":hourglass_flowing_sand: starting",
        "available": "[grey]:heavy_check_mark:[/grey] available",
        "error": "[red]:heavy_multiplication_x:[/red] error",
    }

    table = Table()
    table.add_column("Service")
    table.add_column("Status")

    services = list(services.items())
    services.sort(key=lambda item: item[0])

    for service, status in services:
        if status in status_display:
            status = status_display[status]

        table.add_row(service, status)

    console.print(table)


def print_version():
    console.print(f" :laptop_computer: [bold]LocalStack CLI[/bold] [blue]{__version__}[/blue]")


def print_profile():
    if config.LOADED_PROFILE:
        console.print(
            f" :bust_in_silhouette: [bold]Profile:[/bold] [blue]{config.LOADED_PROFILE}[/blue]"
        )


def print_error(format, error):
    if format == "table":
        symbol = "[bold][red]:heavy_multiplication_x: ERROR[/red][/bold]"
        console.print(f"{symbol}: {error}")
    if format == "plain":
        console.print(f"error={error}")
    if format == "dict":
        console.print({"error": error})
    if format == "json":
        console.print(json.dumps({"error": error}))


def print_banner():
    print(BANNER)


def is_frozen_bundle() -> bool:
    """
    :return: true if we are currently running in a frozen bundle / a pyinstaller binary.
    """
    # check if we are in a PyInstaller binary
    # https://pyinstaller.org/en/stable/runtime-information.html
    return getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS")
