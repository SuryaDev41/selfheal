"""Configuration loader for LLM providers"""

import os
from pathlib import Path
from dotenv import load_dotenv

# Load environment variables from .env file
env_file = Path(".env.openai")
if env_file.exists():
    load_dotenv(env_file)
else:
    load_dotenv(".env")

def get_openai_client():
    """Get OpenAI client"""
    try:
        from openai import OpenAI
        
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise ValueError("OPENAI_API_KEY not found in environment")
        
        return OpenAI(api_key=api_key)
    except ImportError:
        raise ImportError("OpenAI client not available. Install with: pip install openai")

def get_anthropic_client():
    """Get Anthropic client"""
    try:
        from anthropic import Anthropic
        
        api_key = os.getenv("ANTHROPIC_API_KEY")
        if not api_key:
            raise ValueError("ANTHROPIC_API_KEY not found in environment")
        
        return Anthropic(api_key=api_key)
    except ImportError:
        raise ImportError("Anthropic client not available. Install with: pip install anthropic")

def load_llm_client(model=None):
    """Load LLM client based on provider"""
    
    # Get provider from environment, DEFAULT to openai
    provider = os.getenv("LLM_PROVIDER", "openai").lower()
    
    print(f"DEBUG: Using LLM_PROVIDER={provider}")
    
    if provider == "openai":
        return get_openai_client()
    elif provider == "anthropic":
        return get_anthropic_client()
    else:
        raise ValueError(f"Unknown LLM provider: {provider}. Use 'openai' or 'anthropic'")

def load_openai_client(model=None):
    """Load OpenAI client"""
    return get_openai_client()

def load_anthropic_client(model=None):
    """Load Anthropic client"""
    return get_anthropic_client()

def get_model_name():
    """Get model name from environment"""
    provider = os.getenv("LLM_PROVIDER", "openai").lower()
    
    if provider == "openai":
        return os.getenv("OPENAI_MODEL", "gpt-3.5-turbo")
    elif provider == "anthropic":
        return os.getenv("ANTHROPIC_MODEL", "claude-3-sonnet-20240229")
    else:
        return "gpt-3.5-turbo"