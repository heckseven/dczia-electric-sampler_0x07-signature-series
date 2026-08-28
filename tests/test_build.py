"""Tests for the deployment build.

What matters here is which files get compiled and which must not. main.py
compiled to bytecode is a badge that does not boot - CircuitPython looks for
main.py and never main.mpy - and that is not a mistake anyone finds by reading
the output, so it is pinned here.

The compiler itself is a separate binary that is deliberately not vendored, so
these test the decisions rather than the compilation.

build lives in Tools/, which conftest puts on the path.
"""

import os

import pytest

import build


def tree(tmp_path, paths):
    """Make a firmware-shaped directory out of a list of relative paths."""
    for relative in paths:
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("X = 1\n")
    return str(tmp_path)


def actions(tmp_path, paths):
    return dict(
        (relative, action) for action, relative in build.plan(tree(tmp_path, paths))
    )


def test_the_entry_point_stays_source(tmp_path):
    """CircuitPython looks for main.py. A main.mpy is simply never read."""
    plan = actions(tmp_path, ["main.py", "sequencer.py"])

    assert plan["main.py"] == "copy"
    assert plan["sequencer.py"] == "compile"


def test_boot_and_code_stay_source_too(tmp_path):
    """Same rule, same reason - the runtime names these files explicitly."""
    plan = actions(tmp_path, ["boot.py", "code.py"])

    assert plan["boot.py"] == "copy"
    assert plan["code.py"] == "copy"


def test_modules_in_packages_are_compiled(tmp_path):
    plan = actions(tmp_path, [os.path.join("engine", "menu.py")])

    assert plan[os.path.join("engine", "menu.py")] == "compile"


def test_the_vendored_libraries_are_left_alone(tmp_path):
    """lib/ is already bytecode; recompiling its source is not ours to do.

    Written with a .py file on purpose. A .mpy or a .wav is copied by the
    general rule whatever directory it sits in, so a test using one would pass
    with the lib/ rule deleted entirely and prove nothing.
    """
    plan = actions(tmp_path, [os.path.join("lib", "adafruit_thing.py")])

    assert plan[os.path.join("lib", "adafruit_thing.py")] == "copy"


def test_samples_are_copied(tmp_path):
    """Same reasoning: a .py under samples/ is the case that distinguishes."""
    plan = actions(
        tmp_path,
        [os.path.join("samples", "kick_crater.wav"), os.path.join("samples", "x.py")],
    )

    assert plan[os.path.join("samples", "kick_crater.wav")] == "copy"
    assert plan[os.path.join("samples", "x.py")] == "copy"


def test_a_nested_directory_named_lib_is_ordinary_source(tmp_path):
    """Only a top-level lib/ is vendored. The rule is deliberately shallow."""
    plan = actions(tmp_path, [os.path.join("engine", "lib", "thing.py")])

    assert plan[os.path.join("engine", "lib", "thing.py")] == "compile"


def test_things_that_are_not_python_are_copied(tmp_path):
    plan = actions(tmp_path, ["settings.toml"])

    assert plan["settings.toml"] == "copy"


def test_caches_are_not_deployed(tmp_path):
    """__pycache__ is the host's, and 300 KB of it would not fit anyway."""
    plan = actions(
        tmp_path, ["sequencer.py", os.path.join("__pycache__", "sequencer.pyc")]
    )

    assert os.path.join("__pycache__", "sequencer.pyc") not in plan


def test_the_badge_version_is_read_from_boot_out(tmp_path):
    """Bytecode from the wrong compiler imports as "Incompatible .mpy file".

    Worth catching before it reaches hardware, where the message arrives with
    no clue as to which of the two versions is the odd one.
    """
    (tmp_path / "boot_out.txt").write_text(
        "Adafruit CircuitPython 10.2.1 on 2026-05-13; "
        "Raspberry Pi Pico W with rp2040\nBoard ID:raspberry_pi_pico_w\n"
    )

    assert build.badge_version(str(tmp_path)) == "10.2.1"


def test_an_unknown_target_has_no_version(tmp_path):
    """Building into a plain directory is normal and must not be refused."""
    assert build.badge_version(str(tmp_path)) is None


def fake_compiler(tmp_path, exit_code=0):
    """Stand in for mpy-cross, so the build can be driven without one."""
    script = tmp_path / "fake-mpy-cross"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "if %d:\n"
        "    sys.stderr.write('nope\\n')\n"
        "    raise SystemExit(%d)\n"
        "open(sys.argv[2], 'w').write('BYTECODE')\n" % (exit_code, exit_code)
    )
    script.chmod(0o755)
    return str(script)


def test_a_build_compiles_the_modules_and_copies_the_rest(tmp_path):
    source = tree(tmp_path / "src", ["main.py", "sequencer.py", "settings.toml"])
    out = str(tmp_path / "out")

    compiled, copied, skipped, before, after = build.build(
        source, out, fake_compiler(tmp_path), verbose=False
    )

    assert (compiled, copied, skipped) == (1, 2, 0)
    assert os.path.exists(os.path.join(out, "sequencer.mpy"))
    assert os.path.exists(os.path.join(out, "main.py")), "the entry point must stay"
    assert not os.path.exists(os.path.join(out, "main.mpy"))
    assert before > 0 and after > 0


def test_a_compiler_that_fails_stops_the_build(tmp_path):
    """A half-built badge is worse than one that was never written."""
    source = tree(tmp_path / "src", ["sequencer.py"])

    with pytest.raises(SystemExit):
        build.build(
            source, str(tmp_path / "out"), fake_compiler(tmp_path, 1), verbose=False
        )


def test_a_symlink_in_the_source_is_not_followed(tmp_path):
    """It would copy whatever it points at onto a card that gets carried around."""
    source = tree(tmp_path / "src", ["real.py"])
    os.symlink("/etc/passwd", os.path.join(source, "sneaky.py"))

    plan = dict((relative, action) for action, relative in build.plan(source))

    assert plan["sneaky.py"] == "skip"
    assert plan["real.py"] == "compile"


def test_it_refuses_to_write_through_a_symlink(tmp_path):
    """The output is often a mounted card that has been out of your hands."""
    out = tmp_path / "out"
    out.mkdir()
    elsewhere = tmp_path / "elsewhere.py"
    elsewhere.write_text("original\n")
    os.symlink(str(elsewhere), str(out / "main.py"))

    with pytest.raises(SystemExit):
        build._target(str(out), "main.py")

    assert elsewhere.read_text() == "original\n", "it wrote through the link"


def test_the_version_is_read_from_either_kind_of_string():
    """boot_out.txt and mpy-cross --version are both free text around a version."""
    badge = "Adafruit CircuitPython 10.2.1 on 2026-05-13; Raspberry Pi Pico W"
    compiler = "CircuitPython 10.2.1 on 2026-05-12; mpy-cross emitting mpy v6.3"

    assert build._version_word(badge) == "10.2.1"
    assert build._version_word(compiler) == "10.2.1"
    assert build._version_word("no version here") is None
