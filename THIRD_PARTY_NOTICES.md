# Third-party notices

RepoPilot's own code (`repopilot/`, `evals/`, `scripts/`, `tests/`, `docs/`)
is licensed under the MIT License in `LICENSE`. RepoPilot-Bench is built on
seven open-source Python libraries, and parts of this repository are derived
from them. Those parts remain under their upstream licenses, reproduced or
referenced below, with the upstream copyright notices retained.

## What is taken from each repository

The libraries themselves are not vendored: every task pins a commit, and the
sandbox clones the repository at build time (`repopilot/sandbox/repo.py`).
What this repository does contain:

- **Bug patches** (`evals/benchmark/sources/<id>/bug.patch`, and their exact
  reverse as `gold_patch` in `evals/benchmark/tasks/<id>.json`): unified
  diffs against upstream source files, so each carries a few lines of the
  upstream code as context and as the changed lines.
- **Hidden regression tests** (`hidden.patch`, `hidden_test_patch`): written
  for this benchmark in the style of the upstream test suites. For the four
  historical-fix tasks (`click_009`, `rich_009`, `jinja_009`, `sqlparse_009`)
  the tests are the upstream project's own regression tests for that fix,
  transplanted from the fix commit, and the gold patch is the source part of
  that upstream commit.
- **Archived traces** (`evals/experiments/*/traces/*.jsonl`): the agent's
  tool calls return excerpts of upstream source and test files verbatim, and
  the memorization-probe archives (`evals/experiments/memorization-v1*`)
  contain model-written reconstructions of upstream functions, scored
  against the originals.
- **Task descriptions** quote upstream public API names and observable
  behaviour; they contain no upstream code beyond short usage snippets.

| repository | pinned commit(s) | license | copyright |
| --- | --- | --- | --- |
| [cachetools](https://github.com/tkem/cachetools) 7.1.8 | `4500e3d04288738d25acbb4973eb3c3e1bf41db9` | MIT | Copyright (c) 2014-2026 Thomas Kemmer |
| [toolz](https://github.com/pytoolz/toolz) 1.1.0 | `568c2b8393973cd172a466546c9d95779c452438` | BSD 3-Clause | Copyright (c) 2013 Matthew Rocklin |
| [tenacity](https://github.com/jd/tenacity) 9.2.0 | `a2af454834c6bb5a1e39d67334031cdaf0f475b5` | Apache License 2.0 | Copyright 2013-2014 Ray Holder; 2016 Joshua Harlow; 2016 Étienne Bersac; 2016-2021 Julien Danjou; 2017 Elisey Zanko (per the upstream source headers) |
| [click](https://github.com/pallets/click) 8.3.0 | `00fadb8904387158ce6e9aa1573be770446895c1`; historical fix `4fd2fea0db` on base `9ce34f20d2` | BSD 3-Clause | Copyright 2014 Pallets |
| [rich](https://github.com/Textualize/rich) 14.1.0 | `2dca1b70359dac61e1bbfb6f14ebe19a5ab79c3d`; historical fix `30e5ed61a6` on base `69e1618f1f` | MIT | Copyright (c) 2020 Will McGugan |
| [Jinja](https://github.com/pallets/jinja) 3.1.6 | `15206881c006c79667fe5154fe80c01c65410679`; historical fix `4c703ec44d` on base `02071b3e59` | BSD 3-Clause | Copyright 2007 Pallets |
| [sqlparse](https://github.com/andialbrecht/sqlparse) 0.5.3 | `ec0af5bf6345750d84274bc5c857d4a75b88619b`; historical fix `44eacf2e2f` on base `aaf5403f05` | BSD 3-Clause | Copyright (c) 2016, Andi Albrecht |

The full license texts are in each repository at the pinned commit
(`LICENSE`, `LICENSE.txt` or `LICENSE.rst`) and at the links above.

## License texts

### MIT License (cachetools, rich)

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.

### BSD 3-Clause License (toolz, click, Jinja, sqlparse)

Redistribution and use in source and binary forms, with or without
modification, are permitted provided that the following conditions are met:

1. Redistributions of source code must retain the above copyright notice,
   this list of conditions and the following disclaimer.
2. Redistributions in binary form must reproduce the above copyright notice,
   this list of conditions and the following disclaimer in the documentation
   and/or other materials provided with the distribution.
3. Neither the name of the copyright holder nor the names of its contributors
   may be used to endorse or promote products derived from this software
   without specific prior written permission.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE
FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY,
OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

### Apache License 2.0 (tenacity)

Licensed under the Apache License, Version 2.0 (the "License"); the
tenacity-derived diffs and excerpts in this repository may not be used except
in compliance with the License. A copy of the License is available at
<http://www.apache.org/licenses/LICENSE-2.0> and in the tenacity repository at
the pinned commit. Unless required by applicable law or agreed to in writing,
software distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See
the License for the specific language governing permissions and limitations
under the License.

## Models and other runtime dependencies

The dense retrieval channel downloads `BAAI/bge-small-en-v1.5` (MIT) through
`fastembed` at first use; it is not redistributed here. Python dependencies
are declared in `pyproject.toml` and pinned in `uv.lock` under their own
licenses.
