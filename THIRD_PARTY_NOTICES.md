# Third-Party Notices

This project incorporates or ports portions of the following open-source software:

## 1. T3 Code (v0.1.11)
- **Upstream**: https://github.com/pingdotgg/t3code
- **Used in**: Quota normalization & usage aggregation logic (`quota_hub.py`, `usage_p.py`), multi-agent delegation conventions (`collab.py`), and Codex / Antigravity subagent status conventions (`prov_agents.py`).
- **License**: MIT License

> MIT License
>
> Copyright (c) 2026 T3 Tools Inc.
>
> Permission is hereby granted, free of charge, to any person obtaining a copy
> of this software and associated documentation files (the "Software"), to deal
> in the Software without restriction, including without limitation the rights
> to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
> copies of the Software, and to permit persons to whom the Software is
> furnished to do so, subject to the following conditions:
>
> The above copyright notice and this permission notice shall be included in all
> copies or substantial portions of the Software.
>
> THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
> IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
> FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
> AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
> LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
> OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
> SOFTWARE.

## 2. LobeHub Icons
- **Upstream**: https://github.com/lobehub/lobe-icons
- **Used in**: `static/provider-icons/antigravity.svg` and inline Codex SVG icon.
- **License**: MIT License, Copyright (c) 2023 LobeHub (full license in `static/provider-icons/LICENSE`).

## 3. Vendored Frontend Libraries (`static/vendor/`)
- **marked** (12.0.2) — MIT License
- **DOMPurify** (3.1.6) — Apache-2.0 / MPL-2.0
- **highlight.js** — BSD-3-Clause
- **qrcode-generator** (1.4.4) — MIT License

## 4. Python Dependency
- **pyte** (and `wcwidth`) — LGPL-3.0 / MIT (installed via `requirements.txt`, not vendored in this repository).
