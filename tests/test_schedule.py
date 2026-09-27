import plistlib
from pathlib import Path

import pytest

from transcoder_bot.schedule import build_agent, parse_days, parse_time, write_agent


def test_parse_time():
    assert parse_time("18:00") == (18, 0)
    assert parse_time(" 7:05 ") == (7, 5)
    for bad in ("18", "25:00", "18:60", "six"):
        with pytest.raises(ValueError, match="18:30"):
            parse_time(bad)


def test_parse_days():
    assert parse_days(None) == []
    assert parse_days("Sunday, wed,sun") == [0, 3]
    with pytest.raises(ValueError, match="Unknown day"):
        parse_days("funday")


def test_daily_agent():
    agent = build_agent(
        program=["/repo/.venv/bin/python", "-m", "transcoder_bot", "poll"],
        hour=18,
        minute=0,
        log_path=Path("/Users/me/Library/Logs/transcoder-bot.log"),
    )

    assert agent["Label"] == "local.transcoder-bot"
    assert agent["ProgramArguments"][0] == "/repo/.venv/bin/python"  # Python itself, see TCC
    assert agent["StartCalendarInterval"] == {"Hour": 18, "Minute": 0}
    assert agent["ProcessType"] == "Interactive"
    assert agent["EnvironmentVariables"]["PATH"].startswith("/opt/homebrew/bin:")
    assert agent["StandardErrorPath"] == agent["StandardOutPath"]


def test_agent_for_some_weekdays(tmp_path):
    agent = build_agent(
        program=["python"], hour=13, minute=30, weekdays=[0, 3], log_path=tmp_path / "log"
    )
    assert agent["StartCalendarInterval"] == [
        {"Weekday": 0, "Hour": 13, "Minute": 30},
        {"Weekday": 3, "Hour": 13, "Minute": 30},
    ]

    path = tmp_path / "LaunchAgents" / "local.transcoder-bot.plist"
    write_agent(agent, path)
    with path.open("rb") as fh:
        assert plistlib.load(fh) == agent
