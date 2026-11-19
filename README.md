# Offline analytics input set

Synthetic meetings, summary revisions, linked project actions, and human review records. All records are synthetic and use UTC where timestamps are present.

## Environment

Python 3.11 or newer; standard library only. No network access is required.

## Baseline check

Run `python -m unittest discover -s tests` from this directory to verify the supplied CSV files can be read and have unique row identifiers.
