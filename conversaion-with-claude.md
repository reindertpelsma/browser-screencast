Massive update is dropped at /workspace/browser-screencast. can you check and find any bugs? Flag them from your current context so I can fix them, like you did before. Still read only for now

Ran
See what changed and current scale
Three commits, one big one ("Initial port") plus two new feature pushes ("Finish Linux headless validation matrix" + "Validate Windows scheduled task capture"). Let me check the six known issues + the new code.


Ran a command, read a file
Issues 1, 2, 3 fixed cleanly. Continuing.


Ran 9 commands, read 3 files
Sweep done. Cleanly: all six issues from yesterday are fixed, plus the keyboard-symbols issue I had only mentioned in passing, plus the dead-code in vnc.py:_restart_screensharingd is now properly platform-gated. Codex did good work overnight.

Yesterday's six issues — verification
#	Issue	Status	How
1	mvs/audio.py Windows WASAPI vs dshow	✅ fixed	Probes ffmpeg -devices and picks wasapi if present, else dshow with BROWSER_SCREENCAST_AUDIO_DEVICE env var
2	_audio_capture_thread pipe stall when no clients	✅ fixed	_has_audio_clients() gate kills the FFmpeg subprocess when clients leave; respawned on reconnect (with terminate → wait(1s) → kill cascade)
3	systemd After=graphical-session.target blocks headless	✅ fixed	Removed; plus separate browser-screencast-headless.service.template
4	VP9 HW asymmetry	✅ fixed	_SERVER_ENCODERS["vp9"]["hw"] = [] with comment explaining why (no hardware-frame upload path in EncoderPipeline yet). Honest fix
5	install.ps1 copies .git/.venv/__pycache__	✅ fixed	$Exclude list + ForEach-Object filter
6	install.ps1 $IsMacOS + no git-clone fallback	✅ fixed	PS-version guard + [Environment]::OSVersion.Platform fallback; git-clone fallback added
Bonus: the shifted-symbol keyboard issue I only mentioned in passing yesterday (XK dict didn't cover !@#$...) is now fixed via _x11_ascii_chord + _SHIFTED_ASCII table in mvs/platform.py:94-104, with explicit regression tests in tests/test_platform_keys.py (3 tests, all passing locally in venv). Including the test "physical Digit1 with key='!' does not inject Shift" — exactly the right test to write because that's the failure mode that would silently produce double-shift on real keyboards.

New bugs introduced overnight (priority order)
Tier 1 — subprocess leaks on failure paths
1. server.py:132-164 — Xvfb / WM leak when _build_bridge raises.

headless = HeadlessSession(cfg)
headless.start()  # spawns Xvfb + openbox
...
bridge = _build_bridge(cfg)  # ← if THIS raises, headless never cleaned up
...
try:
    asyncio.run(_main(cfg, bridge))
finally:
    bridge.stop()
    if headless is not None:
        headless.stop()
If _build_bridge(cfg) raises (e.g. mss can't open the freshly-spawned Xvfb display because of authority issues, or the bridge constructor fails for any reason), the Xvfb + window-manager processes spawned by headless.start() are not cleaned up — the finally only wraps asyncio.run, not the bridge build. Fix: move the bridge-build inside the try block, or use a separate try/finally that wraps from headless.start() onward.

2. mvs/headless.py:start() — partial-spawn leak when _wait_display times out.

self._procs.append(subprocess.Popen(cmd, ...))
self._started_xvfb = True
self._wait_display()   # raises after 5s if Xvfb isn't ready
If Xvfb is Popen'd successfully but doesn't become responsive within 5 seconds, _wait_display() raises RuntimeError. The Xvfb process is already in self._procs, but start()'s exception propagates back to server.py, which then doesn't reach the finally block because asyncio.run never starts. Xvfb subprocess is orphaned. Fix: wrap _wait_display in try/except that calls self.stop() on failure before re-raising.

Tier 2 — UX / silent-failure bugs
3. mvs/headless.py:_select_wm — WM spawn isn't verified.

self._procs.append(subprocess.Popen(wm_cmd, ...))
time.sleep(0.5)   # ← just hopes the WM is alive
If openbox / xfwm4 / fluxbox exits immediately (config error, missing dep, missing ~/.config/openbox/rc.xml), the user silently gets a bare X11 display. Add proc.poll() is None check after the sleep; if it's already exited, log a warning and continue without WM (or fall through to the next candidate).

4. mvs/headless.py:start() — os.environ["DISPLAY"] mutated before Xvfb is verified.

Line 22 sets os.environ["DISPLAY"] = self.display before the Xvfb subprocess is Popen'd. If Xvfb fails (binary missing — handled by the which check, but also: out-of-memory, permission, Xauthority misconfig), the env mutation persists for the rest of the Python process. Subsequent fallback logic (e.g. anything that re-runs detect_capture_mode) will see DISPLAY=:99 and assume X11 is available. Fix: only set DISPLAY after the display is verified ready.

5. mvs/platform.py Clipboard.set + Windows Get-Clipboard — round-trip ping-pong.

Windows PowerShell's Get-Clipboard always appends a trailing \r\n to the output. After set("foo") succeeds, the 1-second _poll() calls get() → returns "foo\r\n" → text != self.text is True → seq++ → frontend gets a clipboard update it didn't ask for, with a different value than what it sent. The browser may then sync that back, which sets "foo\r\n" on the Mac, which Get-Clipboard returns as "foo\r\n\r\n", and the loop drifts. Fix: _powershell_clip("Get-Clipboard").rstrip("\r\n") or normalize on compare.

Tier 3 — minor / cosmetic
6. server.py:54-55 — HEADLESS env var only honors "1".

default=os.environ.get("HEADLESS", "0") == "1",
Doesn't accept "true", "yes", "True" — common conventions. Trivial fix: os.environ.get("HEADLESS", "0").lower() in ("1", "true", "yes").

7. install.sh:84 — WM availability is checked at install time only.

If a user runs --headless install while openbox is present, then later uninstalls openbox, runtime falls through mvs/headless.py:_select_wm() to "no WM found" and silently gives bare X11. Not really a bug, but the install-time check creates a false expectation.

Things that look genuinely good in the new code
tests/test_platform_keys.py — the regression tests for the keyboard fix are exactly the right tests. The "physical Digit1 + key='!' does NOT inject Shift" case is the precise failure mode that would otherwise cause silent double-shift in real-world typing. Good test design.
tests/test_codec_probe.py — clean unit-test pattern, replaces module-level globals (codec._av, codec._AV_OK, etc.) in setUp/tearDown with cache_clear() in the right places. Proper isolation.
tests/linux_matrix.py — 276 lines of structured matrix testing with strict per-step subprocess management, MatrixFailure exception, and _run/_spawn helpers. This is the right shape for tier-2/3 testing.
mvs/headless.py:_wait_display uses xdpyinfo to verify display readiness rather than just sleeping — proper readiness-check.
install.ps1 ScheduledTask now uses LogonType Interactive with explicit $env:COMPUTERNAME\$env:USERNAME principal — that's the right config for "task must run in the desktop session," with a comment explaining why.
VP9 honest fix — comment explains the encoder cascade can't open VP9 HW yet, so don't advertise it. That's the right way to handle "this is broken; document and disable" rather than a TODO that drifts.
Summary
Codex shipped a high-quality overnight push: yesterday's six issues all properly addressed (not just patched but with regression tests where appropriate), plus a clean Phase 4b headless implementation. The new bugs are all in the same category — subprocess-lifecycle failure paths (server.py + headless.py) and clipboard normalization on Windows. Nothing structurally wrong with the new architecture; just needs the failure-path cleanup that's typical when a feature lands.

Priority for fix-pass when you're ready: bugs 1 and 2 first (real subprocess leaks on the headless failure path — would surface in production). Then 5 (Windows clipboard ping-pong, will manifest the moment a real Windows user pastes anything containing a newline). The rest can wait.
