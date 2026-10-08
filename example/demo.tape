# Demo GIF for the README: the example from EXAMPLE.md, recorded with charmbracelet/vhs.
# Run from the repository root: vhs example/demo.tape  (writes example/demo.gif)
# Needs: vhs, claude (logged in), git, python3. Uses the plugin from this checkout (--plugin-dir).
# The model may read the tests and pass the first time; then the log shows a single PASS. Re-record if so.

Output example/demo.gif
Require claude
Require git
Require python3

Set Shell "bash"
Set FontSize 16
Set Width 1280
Set Height 720
Set Padding 20
Set TypingSpeed 35ms
Set WaitTimeout 240s

Hide
Type `export KIT="$PWD" PS1='$ ' && rm -rf /tmp/prove-it-demo && cp -R example/shop /tmp/prove-it-demo && cd /tmp/prove-it-demo`
Enter
Type `git init -q && git add -A && git commit -qm base && python3 -m venv .venv && .venv/bin/pip install -q pytest && clear`
Enter
Wait /\$ $/
Show

Type "# a small shop: 3 new tests for discount code HALF, not implemented yet"
Enter
Type ".venv/bin/python -m pytest -q | tail -1"
Enter
Wait /\$ $/
Sleep 2s

Type `claude -p "/prove-it-mini:setup" --plugin-dir "$KIT" --model haiku`
Enter
Wait /\$ $/
Sleep 2s

Type `claude -p "In shop/pricing.py add discount code HALF: 50% off for orders whose subtotal is over 100. Do not open or run anything in tests/; edit only shop/pricing.py and finish quickly." --plugin-dir "$KIT" --model haiku --permission-mode acceptEdits`
Enter
Wait /\$ $/
Sleep 2s

Type "cat .prove-it/gate-log.md"
Enter
Sleep 4s
Type ".venv/bin/python -m pytest -q | tail -1"
Enter
Wait /\$ $/
Sleep 5s
