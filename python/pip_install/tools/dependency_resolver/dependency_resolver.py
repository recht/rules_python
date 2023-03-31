# Copyright 2023 The Bazel Authors. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"Set defaults for the pip-compile command to run it under Bazel"

import atexit
import os
import shutil
import sys
from pathlib import Path

import piptools.writer as piptools_writer
from piptools.scripts.compile import cli

# Replace the os.replace function with shutil.copy to work around os.replace not being able to
# replace or move files across filesystems.
os.replace = shutil.copy

# Next, we override the annotation_style_split and annotation_style_line functions to replace the
# backslashes in the paths with forward slashes. This is so that we can have the same requirements
# file on Windows and Unix-like.
original_annotation_style_split = piptools_writer.annotation_style_split
original_annotation_style_line = piptools_writer.annotation_style_line


def annotation_style_split(required_by) -> str:
    required_by = set([v.replace("\\", "/") for v in required_by])
    return original_annotation_style_split(required_by)


def annotation_style_line(required_by) -> str:
    required_by = set([v.replace("\\", "/") for v in required_by])
    return original_annotation_style_line(required_by)


piptools_writer.annotation_style_split = annotation_style_split
piptools_writer.annotation_style_line = annotation_style_line


def _select_golden_requirements_file(
    requirements_txt, requirements_linux, requirements_darwin, requirements_windows
):
    """Switch the golden requirements file, used to validate if updates are needed,
    to a specified platform specific one.  Fallback on the platform independent one.
    """

    plat = sys.platform
    if plat == "linux" and requirements_linux is not None:
        return requirements_linux
    elif plat == "darwin" and requirements_darwin is not None:
        return requirements_darwin
    elif plat == "win32" and requirements_windows is not None:
        return requirements_windows
    else:
        return requirements_txt


if __name__ == "__main__":
    if len(sys.argv) < 4:
        print(
            "Expected at least two arguments: requirements_in requirements_out",
            file=sys.stderr,
        )
        sys.exit(1)

    parse_str_none = lambda s: None if s == "None" else s

    requirements_in = sys.argv.pop(1)
    requirements_txt = sys.argv.pop(1)
    requirements_linux = parse_str_none(sys.argv.pop(1))
    requirements_darwin = parse_str_none(sys.argv.pop(1))
    requirements_windows = parse_str_none(sys.argv.pop(1))
    update_target_label = sys.argv.pop(1)

    # The requirements_in file could be generated, so we will need to remove the
    # absolute prefixes in the locked requirements output file.
    requirements_in_path = Path(requirements_in)
    resolved_requirements_in = str(requirements_in_path.resolve())

    # Before loading click, set the locale for its parser.
    # If it leaks through to the system setting, it may fail:
    # RuntimeError: Click will abort further execution because Python 3 was configured to use ASCII
    # as encoding for the environment. Consult https://click.palletsprojects.com/python3/ for
    # mitigation steps.
    os.environ["LC_ALL"] = "C.UTF-8"
    os.environ["LANG"] = "C.UTF-8"

    UPDATE = True
    # Detect if we are running under `bazel test`.
    if "TEST_TMPDIR" in os.environ:
        UPDATE = False
        # pip-compile wants the cache files to be writeable, but if we point
        # to the real user cache, Bazel sandboxing makes the file read-only
        # and we fail.
        # In theory this makes the test more hermetic as well.
        sys.argv.append("--cache-dir")
        sys.argv.append(os.environ["TEST_TMPDIR"])
        # Make a copy for pip-compile to read and mutate.
        requirements_out = os.path.join(
            os.environ["TEST_TMPDIR"], os.path.basename(requirements_txt) + ".out"
        )
        # Those two files won't necessarily be on the same filesystem, so we can't use os.replace
        # or shutil.copyfile, as they will fail with OSError: [Errno 18] Invalid cross-device link.
        shutil.copy(requirements_txt, requirements_out)

    update_command = os.getenv("CUSTOM_COMPILE_COMMAND") or "bazel run %s" % (
        update_target_label,
    )

    os.environ["CUSTOM_COMPILE_COMMAND"] = update_command
    os.environ["PIP_CONFIG_FILE"] = os.getenv("PIP_CONFIG_FILE") or os.devnull

    sys.argv.append("--output-file")
    sys.argv.append(requirements_txt if UPDATE else requirements_out)
    sys.argv.append(
        requirements_in if requirements_in_path.exists() else resolved_requirements_in
    )

    if UPDATE:
        print("Updating " + requirements_txt)
        if "BUILD_WORKSPACE_DIRECTORY" in os.environ:
            workspace = os.environ["BUILD_WORKSPACE_DIRECTORY"]
            requirements_txt_tree = os.path.join(workspace, requirements_txt)
            # In most cases, requirements_txt will be a symlink to the real file in the source tree.
            # If symlinks are not enabled (e.g. on Windows), then requirements_txt will be a copy,
            # and we should copy the updated requirements back to the source tree.
            if not os.path.samefile(requirements_txt, requirements_txt_tree):
                atexit.register(
                    lambda: shutil.copy(requirements_txt, requirements_txt_tree)
                )
        cli()
    else:
        # cli will exit(0) on success
        try:
            print("Checking " + requirements_txt)
            cli()
            print("cli() should exit", file=sys.stderr)
            sys.exit(1)
        except SystemExit as e:
            if e.code == 2:
                print(
                    "pip-compile exited with code 2. This means that pip-compile found "
                    "incompatible requirements or could not find a version that matches "
                    f"the install requirement in {requirements_in}.",
                    file=sys.stderr,
                )
                sys.exit(1)
            elif e.code == 0:
                golden_filename = _select_golden_requirements_file(
                    requirements_txt,
                    requirements_linux,
                    requirements_darwin,
                    requirements_windows,
                )
                golden = open(golden_filename).readlines()
                out = open(requirements_out).readlines()
                if golden != out:
                    import difflib

                    print("".join(difflib.unified_diff(golden, out)), file=sys.stderr)
                    print(
                        "Lock file out of date. Run '"
                        + update_command
                        + "' to update.",
                        file=sys.stderr,
                    )
                    sys.exit(1)
                sys.exit(0)
            else:
                print(
                    f"pip-compile unexpectedly exited with code {e.code}.",
                    file=sys.stderr,
                )
                sys.exit(1)
