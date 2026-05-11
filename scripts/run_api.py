"""Entry point: starts the Flask development server."""
import sys
from pathlib import Path

# Ensure src/ is on the path when run directly from scripts/
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from app.api.flask_app import create_app

if __name__ == "__main__":
    flask_app = create_app()
    flask_app.run(host="0.0.0.0", port=5000, debug=True)
