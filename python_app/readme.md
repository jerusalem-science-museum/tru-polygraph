this prototype assumes ideal conditions.

Full documentation (serial protocol, saved CSV columns, GSR and heart-rate filters): [docs/](../docs/README.md).

Install dependencies with `pip install -r python_app/requirements.txt`, then run `python python_app/main.py`. Packaged Windows builds: [docs/releasing.md](../docs/releasing.md).

Each finished examination is saved to `measurements/` as three CSVs (voltage, questions, heart rate) plus an image of the examination graph, `<subject>_<date>_<time>_plot.png`.
