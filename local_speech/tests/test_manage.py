def test_stop_keeps_models_and_other_services(tmp_path):
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location("manage", Path(__file__).parents[1] / "scripts/manage.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.launchctl_stop_command() == ["launchctl", "bootout", f"gui/{module.os.getuid()}/com.ha-ai-tools.speech"]
    assert "remove" not in " ".join(module.launchctl_stop_command())


def test_restart_waits_for_launchd_unload_without_touching_other_jobs(monkeypatch, tmp_path):
    import importlib.util
    from pathlib import Path
    from types import SimpleNamespace

    spec = importlib.util.spec_from_file_location("manage_restart", Path(__file__).parents[1] / "scripts/manage.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    commands, delays = [], []

    def run(command, **kwargs):
        commands.append(command)
        return SimpleNamespace(returncode=5 if len(commands) == 2 else 0)

    monkeypatch.setattr(module.subprocess, "run", run)
    monkeypatch.setattr(module, "_restart_delay", delays.append, raising=False)
    module.launchctl_start(tmp_path / "own.plist")
    assert commands[0] == module.launchctl_stop_command()
    assert len(commands) == 3 and commands[1] == commands[2]
    assert commands[1][0:2] == ["launchctl", "bootstrap"]
    assert delays and sum(delays) <= 6
