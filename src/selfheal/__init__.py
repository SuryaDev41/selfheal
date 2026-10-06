"""
Self-Healing Test Automation Framework

An AI-powered test automation framework that automatically identifies and repairs
broken test elements using Claude (Anthropic) or ChatGPT (OpenAI).

Features:
  - Automatic element identification using AI
  - Element self-healing when UI changes
  - Intelligent caching (99% token reduction)
  - Multi-provider LLM support (Anthropic + OpenAI)
  - Excel-driven test execution
  - Comprehensive logging and cost tracking
  - Safety features (PII masking, domain allow-listing)

Usage:
  from selfheal import framework
  
  framework.run_tests('test_suite.xlsx', env='prod')

Installation:
  pip install -e .
  pip install -r requirements.txt
  playwright install chromium

Configuration:
  export OPENAI_API_KEY=sk-...  (for OpenAI)
  export ANTHROPIC_API_KEY=sk-ant-...  (for Anthropic)
  export LLM_PROVIDER=openai  (or anthropic)

Version: 1.0.0
License: MIT
"""

__version__ = "1.0.0"
__author__ = "AI Test Automation Team"
__license__ = "MIT"

# Version info
VERSION = "1.0.0"
BUILD = "2026-10-03"

# Provider support
SUPPORTED_PROVIDERS = ["anthropic", "openai"]
DEFAULT_PROVIDER = "anthropic"

# Python version requirement
PYTHON_MINIMUM = "3.11"
