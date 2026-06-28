#!/usr/bin/env python3
"""
GitHub Issue Management Agent — knowledgebuilder
Entry point: wraps agents/run_agent.py (v11)

Usage:
  python3 issue_agent.py
  REPO_PATH=/data/workspace/knowledgebuilder python3 issue_agent.py
"""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "agents"))

from run_agent import main  # noqa: E402

if __name__ == "__main__":
    result = main()
    import json
    print("\n결과:", json.dumps(result, ensure_ascii=False, indent=2))
