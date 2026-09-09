"""Evaluation subsystem (§9).

Not a phase-end activity: no feature after this point ships without a
measurement, and `.github/workflows/eval.yml` fails a build on a drop of more
than two points in NDCG@10.
"""
