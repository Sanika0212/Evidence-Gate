"""Write full Prometheus and Grafana Helm values: the base values plus our SLO rules
and dashboard, so the local cluster and EKS install the same monitoring.

    python scripts/render_obs_values.py OUT_DIR
"""

import json
import sys
from pathlib import Path

import yaml

root = Path(__file__).resolve().parents[1] / "deploy" / "observability"
out = Path(sys.argv[1])
out.mkdir(parents=True, exist_ok=True)
prom = yaml.safe_load((root / "prometheus-values.yaml").read_text())
prom.setdefault("serverFiles", {})["recording_rules.yml"] = yaml.safe_load(
    (root / "prometheus-rules.yml").read_text()
)
(out / "prometheus.yaml").write_text(yaml.safe_dump(prom, sort_keys=False))
graf = yaml.safe_load((root / "grafana-values.yaml").read_text())
graf["dashboards"] = {
    "evidencegate": {
        "evidencegate": {
            "json": json.dumps(
                json.loads((root / "grafana/dashboards/evidencegate.json").read_text())
            )
        }
    }
}
(out / "grafana.yaml").write_text(yaml.safe_dump(graf, sort_keys=False))
print(f"wrote {out}/prometheus.yaml and {out}/grafana.yaml")
