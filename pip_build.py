#!/usr/bin/env python3
"""Build the pinned Brave PiP modification using the native host toolchain."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parent
UPSTREAM = json.loads((ROOT / "upstream.json").read_text(encoding="utf-8"))


class BuildError(Exception):
    pass


def capture(args, cwd=None):
    result = subprocess.run(
        [str(a) for a in args], cwd=cwd, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    if result.returncode:
        raise BuildError(result.stderr.strip() or f"Command failed: {args[0]}")
    return result.stdout.strip()


def version(value):
    match = re.search(r"(\d+)\.(\d+)\.(\d+)", value)
    if not match:
        raise BuildError(f"Cannot parse version: {value}")
    return tuple(map(int, match.groups()))


def host_target():
    if sys.platform == "win32":
        return "win", "x64"
    if sys.platform == "darwin":
        return "mac", "arm64" if platform.machine() == "arm64" else "x64"
    raise BuildError("This project currently supports macOS and Windows hosts.")


class Project:
    def __init__(self, build_root, arch=None, jobs=6):
        self.root = Path(build_root).resolve()
        self.src = self.root / "src"
        self.core = self.src / "brave"
        self.target_os, host_arch = host_target()
        self.arch = arch or host_arch
        self.jobs = jobs
        self.pnpm = shutil.which("pnpm")

    def run(self, args, cwd=None, env=None, label="command"):
        args = [str(a) for a in args]
        print("\n> " + " ".join(args), flush=True)
        logs = self.root / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        log = logs / f"{time.strftime('%Y%m%d-%H%M%S')}-{label}.log"
        with log.open("w", encoding="utf-8") as output:
            proc = subprocess.Popen(
                args, cwd=cwd or self.core, env=env,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace", bufsize=1,
            )
            try:
                for line in proc.stdout:
                    print(line, end="", flush=True)
                    output.write(line)
                    output.flush()
                code = proc.wait()
            except KeyboardInterrupt:
                proc.terminate()
                proc.wait()
                raise
            finally:
                proc.stdout.close()
        if code:
            raise BuildError(f"{label} failed ({code}). Log: {log}")

    def doctor(self):
        for tool in ("git", "node", "pnpm"):
            if not shutil.which(tool):
                raise BuildError(f"Install {tool}, reopen the terminal, and retry.")
        git = capture(["git", "--version"])
        node = capture(["node", "--version"])
        pnpm = capture([self.pnpm, "--version"])
        if version(git) < (2, 41, 0):
            raise BuildError("Git 2.41 or newer is required.")
        if version(node)[0] != UPSTREAM["node_major"] or version(node) < version(UPSTREAM["node_min"]):
            raise BuildError("Use Node 24.16.0 or newer within the 24.x release line.")
        if version(pnpm) != version(UPSTREAM["pnpm_version"]):
            raise BuildError(f"Use pnpm {UPSTREAM['pnpm_version']}: npm install -g pnpm@{UPSTREAM['pnpm_version']}")
        print(f"{git}; Node {node}; pnpm {pnpm}")
        print(f"Build: {self.target_os}/{self.arch}; jobs: {self.jobs}; root: {self.root}")
        if " " in str(self.root):
            raise BuildError("Choose a build directory without spaces (Windows example: C:\\pip-build).")
        probe = self.root
        while not probe.exists():
            probe = probe.parent
        print(f"Free disk space: {shutil.disk_usage(probe).free / 2**30:.0f} GiB")
        if self.target_os == "mac":
            print(capture(["xcodebuild", "-version"]))
            print("macOS SDK:", capture(["xcrun", "--show-sdk-version"]))
            try:
                print(capture(["xcrun", "metal", "--version"]))
            except BuildError as error:
                raise BuildError("Metal Toolchain is unavailable. Run xcodebuild -downloadComponent MetalToolchain, then retry doctor.") from error
        else:
            developer_mode = capture([
                "reg.exe", "query",
                r"HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\AppModelUnlock",
                "/v", "AllowDevelopmentWithoutDevLicense",
            ])
            if not re.search(r"REG_DWORD\s+0x1\b", developer_mode):
                raise BuildError("Enable Windows Developer Mode in Settings, then retry.")
            vswhere = Path(os.environ.get("ProgramFiles(x86)", "C:/Program Files (x86)")) / "Microsoft Visual Studio/Installer/vswhere.exe"
            if not vswhere.is_file():
                raise BuildError("Install Visual Studio with Desktop development with C++, ATL/MFC and the required Windows SDK. See README.md.")
            installation = capture([
                vswhere, "-latest", "-products", "*", "-version", "[18.0,)", "-requires",
                "Microsoft.VisualStudio.Component.VC.Tools.x86.x64",
                "Microsoft.VisualStudio.Component.VC.ATLMFC",
                "-property", "installationPath",
            ])
            if not installation:
                raise BuildError("Visual Studio 2026 with C++ x64 and ATL/MFC tools was not found.")
            print("Visual Studio:", installation)
            print("The pinned Chromium hooks will also validate the SDK/toolchain versions.")

    def verify_checkout(self):
        if not (self.core / ".git").exists():
            raise BuildError("Run bootstrap first.")
        actual = capture(["git", "rev-parse", "HEAD"], self.core)
        if actual != UPSTREAM["commit"]:
            raise BuildError(f"Unexpected brave-core revision {actual}; expected {UPSTREAM['commit']}. No files changed.")

    def environment(self):
        if not self.pnpm:
            raise BuildError("Install pnpm and reopen the terminal before building.")
        env = os.environ.copy()
        # Brave consumes --ninja=j but only applies it with remote execution.
        # Set Siso's local limit directly so --jobs also works offline.
        limits = [item.strip() for item in env.get("SISO_LIMITS", "").split(",")
                  if item.strip() and item.partition("=")[0].strip() != "local"]
        limits.append(f"local={self.jobs}")
        env["SISO_LIMITS"] = ",".join(limits)
        # Keep the repair local to this command and its nested pnpm installs.
        shim_dir = self.root / "pnpm-shim"
        shim_dir.mkdir(parents=True, exist_ok=True)
        script = ROOT / "scripts" / "pnpm_compat.py"
        if self.target_os == "win":
            text = f'@echo off\r\n"{sys.executable}" "{script}" %*\r\nexit /b %errorlevel%\r\n'
            (shim_dir / "pnpm.cmd").write_bytes(text.encode("utf-8"))
        else:
            import shlex
            shim = shim_dir / "pnpm"
            shim.write_text(f'#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(str(script))} "$@"\n', encoding="utf-8")
            shim.chmod(0o755)
        env["PIP_REAL_PNPM"] = self.pnpm
        env["PATH"] = str(shim_dir) + os.pathsep + env.get("PATH", "")
        if self.target_os == "win":
            env["DEPOT_TOOLS_WIN_TOOLCHAIN"] = "0"
        return env

    def bootstrap(self):
        self.doctor()
        if not (self.core / ".git").exists():
            if self.core.exists() and any(self.core.iterdir()):
                raise BuildError(f"Refusing to initialize a nonempty directory: {self.core}")
            self.core.mkdir(parents=True, exist_ok=True)
            self.run(["git", "init"], label="git-init")
            self.run(["git", "config", "core.autocrlf", "false"], label="git-config")
            self.run(["git", "config", "core.longpaths", "true"], label="git-config")
            self.run(["git", "remote", "add", "origin", UPSTREAM["repository"]], label="git-remote")
        head = subprocess.run(["git", "rev-parse", "--verify", "HEAD"], cwd=self.core,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if head.returncode:
            if any(p.name != ".git" for p in self.core.iterdir()):
                raise BuildError("Incomplete checkout contains files; refusing to overwrite them.")
            remote = capture(["git", "remote", "get-url", "origin"], self.core)
            if remote != UPSTREAM["repository"]:
                raise BuildError("Incomplete checkout has an unexpected remote.")
            self.run(["git", "fetch", "--depth", "1", "origin", UPSTREAM["commit"]], label="git-fetch")
            self.run(["git", "checkout", "--detach", "FETCH_HEAD"], label="git-checkout")
        self.verify_checkout()
        dirty = capture(["git", "status", "--porcelain", "--untracked-files=no"], self.core)
        if dirty:
            raise BuildError("brave-core has tracked changes. Bootstrap only operates on a clean checkout; it never resets your edits.")
        self.run([
            self.pnpm, "run", "init", "--no-history",
            f"--target_os={self.target_os}", f"--target_arch={self.arch}",
        ], env=self.environment(), label="bootstrap")
        (self.root / "bootstrap.json").write_text(json.dumps({
            "commit": UPSTREAM["commit"], "target_os": self.target_os,
            "arch": self.arch,
        }, indent=2) + "\n", encoding="utf-8")

    def patch(self):
        self.verify_checkout()
        patch_file = ROOT / "patches" / "firefox-pip.patch"
        if not patch_file.is_file():
            raise BuildError("PiP patch is missing.")
        reverse = subprocess.run(
            ["git", "apply", "--reverse", "--check", str(patch_file)],
            cwd=self.core, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        if reverse.returncode == 0:
            print("PiP patch is already applied.")
            return
        self.run(["git", "apply", "--check", patch_file], label="patch-check")
        self.run(["git", "apply", patch_file], label="patch-apply")

    def output_dir(self, config):
        return self.src / "out" / f"Pip{config}-{self.arch}"

    def build(self, config="Component", package=False, baseline=False, target=None):
        self.verify_checkout()
        receipt = self.root / "bootstrap.json"
        if not receipt.exists():
            raise BuildError("Bootstrap did not complete. Run bootstrap and resolve its first error before building.")
        data = json.loads(receipt.read_text(encoding="utf-8"))
        if (data["commit"], data["target_os"], data["arch"]) != (UPSTREAM["commit"], self.target_os, self.arch):
            raise BuildError("Bootstrap configuration differs; use a separate build root for each host/architecture.")
        if baseline:
            if capture(["git", "status", "--porcelain", "--untracked-files=no"], self.core):
                raise BuildError("Baseline build requires an unmodified checkout.")
        else:
            self.patch()
        args = [
            self.pnpm, "run", "build", config,
            "-C", self.output_dir(config),
            f"--target_os={self.target_os}", f"--target_arch={self.arch}",
            "--channel=nightly", "--use_remoteexec=false", "--skip_signing",
            "--gn=symbol_level:0", "--gn=enable_updater:false",
            "--gn=enable_update_notifications:false",
        ]
        if package:
            args.append("--target=create_dist")
        elif target:
            args.append(f"--target={target}")
        self.run(args, env=self.environment(), label="package" if package else "build")
        print("Build output:", self.output_dir(config))

    def launch(self, config):
        out = self.output_dir(config)
        if self.target_os == "win":
            binary = out / "brave.exe"
        else:
            binary = out / "Brave Browser Nightly.app/Contents/MacOS/Brave Browser Nightly"
        if not binary.is_file():
            raise BuildError(f"Browser executable not found: {binary}. Complete build first.")
        profile = self.root / "profiles" / "pip-test"
        self.run([
            binary, f"--user-data-dir={profile}", "--no-first-run",
            "--no-default-browser-check",
        ], cwd=out, label="browser")

    def test(self, config):
        self.build(config, target="brave_unit_tests")
        executable = self.output_dir(config) / ("brave_unit_tests.exe" if self.target_os == "win" else "brave_unit_tests")
        self.run([executable, "--gtest_filter=FirefoxPipTest.*",
                  "--test-launcher-jobs=1", "--test-launcher-bot-mode"],
                 cwd=self.output_dir(config), label="pip-tests")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["doctor", "bootstrap", "apply", "build", "package", "run", "test"])
    parser.add_argument("--build-root", type=Path, default=ROOT / ".build")
    parser.add_argument("--arch", choices=["x64", "arm64"])
    parser.add_argument("--jobs", type=int, default=6)
    parser.add_argument("--config", choices=["Component", "Static", "Debug"], default=None)
    parser.add_argument("--baseline", action="store_true")
    parser.add_argument("--target", help="Optional native build target, e.g. brave_unit_tests")
    args = parser.parse_args()
    if args.jobs < 1:
        parser.error("--jobs must be positive")
    if args.baseline and args.command != "build":
        parser.error("--baseline is only valid for build")
    project = Project(args.build_root, args.arch, args.jobs)
    config = args.config or ("Static" if args.command == "package" else "Component")
    if args.command == "doctor":
        project.doctor()
    elif args.command == "bootstrap":
        project.bootstrap()
    elif args.command == "apply":
        project.patch()
    elif args.command in ("build", "package"):
        project.build(config, args.command == "package", args.baseline, args.target)
    elif args.command == "test":
        project.test(config)
    else:
        project.launch(config)


if __name__ == "__main__":
    try:
        main()
    except (BuildError, OSError) as error:
        print(f"\nERROR: {error}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("\nInterrupted. Downloaded data and build output have been kept.", file=sys.stderr)
        sys.exit(130)
