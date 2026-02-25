import sys
from pathlib import Path

# Add workspace root so tests can import project modules
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
