# Reference: Rally Cutter detector (JavaScript)

`detect.js` is copied verbatim from the original browser app
(`martinzukowski/vball`, commit `a6b001d`). It is **not** used by the app.

It exists so `services/worker/tests/test_heuristic_parity.py` can run the original and the
Python port (`vball_worker/rally/heuristic.py`) on the same feature sequences and check they
produce the same rallies. Don't edit it; change the Python port and update the test instead.
