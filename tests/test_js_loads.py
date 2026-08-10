import unittest
import os
import re
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["OPENROUTER_API_KEY"] = "sk-or-v1-test"

APP_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app.py")

DOM_STUB = r"""
const fakeEl = () => ({ id:'', dataset:{}, value:'', innerHTML:'', style:{}, classList:{add(){},remove(){},toggle(){},contains(){return false}}, addEventListener(){}, appendChild(){}, querySelector(){return null}, querySelectorAll(){return []}, contains(){return false}, removeChild(){}, insertBefore(){}, setAttribute(){}, focus(){}, click(){}, scrollIntoView(){}, checked:false, disabled:false });
global.document = {
  getElementById: () => fakeEl(),
  querySelector: () => null,
  querySelectorAll: () => [],
  addEventListener(){}, removeEventListener(){},
  createElement: () => fakeEl(),
  body:{classList:{add(){},remove(){},toggle(){}},appendChild(){}},
  visibilityState:'visible',
  title:''
};
global.window = { AudioContext: function(){}, webkitAudioContext: undefined, addEventListener(){}, removeEventListener(){}, innerWidth:1000 };
global.navigator = { userAgent:'test' };
global.fetch = (u,o) => Promise.reject(new Error('no network'));
global.localStorage = { getItem:()=>null, setItem(){}, removeItem(){} };
global.setTimeout = ()=>{};
global.setInterval = ()=>{};
global.clearInterval = ()=>{};
global.location = { href:'' };
global.Notification = function(){};
global.requestAnimationFrame = ()=>{};
"""


def _extract_inline_js():
    src = open(APP_PATH).read()
    m = re.search(r'FRONTEND_HTML = r"""(.*?)"""', src, re.S)
    assert m, "FRONTEND_HTML not found"
    html = m.group(1)
    matches = re.findall(r'<script>(.*?)</script>', html, re.S)
    inline = [s for s in matches if "<script" not in s and not s.strip().startswith("src=")]
    assert len(inline) == 1, f"expected 1 inline script block, found {len(inline)}"
    return inline[0]


class TestJSLoads(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.node = shutil.which("node")
        if not cls.node:
            raise unittest.SkipTest("node not available")

    def test_inline_js_is_valid_syntax(self):
        js = _extract_inline_js()
        with tempfile.NamedTemporaryFile(suffix=".js", delete=False) as f:
            f.write(js.encode())
            path = f.name
        try:
            r = subprocess.run([self.node, "--check", path], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, f"JS syntax error:\n{r.stderr}")
        finally:
            os.unlink(path)

    def test_inline_js_executes_top_level_without_error(self):
        """Catches TDZ/ReferenceError-style bugs that kill the whole script at load
        (e.g. calling a function that touches a `let` var before its declaration)."""
        js = _extract_inline_js()
        with tempfile.NamedTemporaryFile(suffix=".js", delete=False) as f:
            f.write((DOM_STUB + "\n" + js).encode())
            path = f.name
        try:
            r = subprocess.run([self.node, path], capture_output=True, text=True, timeout=30)
            self.assertEqual(r.returncode, 0, f"JS crashed during load:\n{r.stderr}\n---\n{r.stdout}")
        finally:
            os.unlink(path)


if __name__ == "__main__":
    unittest.main()