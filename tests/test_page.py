"""
The page's own script must parse. A syntax error anywhere in it stops the
whole map from loading — and every Python test still passes, because none of
them run the browser code. That happened while adding multi-stop trips: one
dropped line in the transit itinerary left a bare `<li>` in the script.
"""

import re
import shutil
import subprocess

import pytest


@pytest.mark.skipif(not shutil.which("node"), reason="needs Node to parse the script")
def test_page_script_parses(client, tmp_path):
    html = client.get("/").get_data(as_text=True)
    scripts = re.findall(r"<script>(.*?)</script>", html, flags=re.S)   # inline only, not CDN ones
    assert scripts, "no inline script found on the page"
    path = tmp_path / "page.js"
    path.write_text("\n;\n".join(scripts))
    result = subprocess.run(["node", "--check", str(path)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
