"""
LLM Usage Logging - Tracks token usage, cost, and API calls
Creates data/logs/ on first run
"""

import json
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional

class UsageLogger:
    """Log LLM calls and usage metrics"""
    
    def __init__(self, run_id: str, app: str = 'automationexercise'):
        self.run_id = run_id
        self.app = app
        
        # CREATE data/logs/ directory on first run
        self.log_dir = Path("data/logs")
        self.log_dir.mkdir(parents=True, exist_ok=True)  # ← KEY LINE: Creates folder!
        
        # Initialize aggregates
        self.total_tokens = 0
        self.total_cost = 0.0
        self.call_count = 0
        self.model_usage = {}
        self.calls = []
    
    def log_llm_call(self, 
                    test_case_id: str,
                    step_no: int,
                    reason: str,  # 'identify', 'heal'
                    model: str,
                    input_tokens: int,
                    output_tokens: int,
                    latency_ms: int,
                    status: str = 'success'):
        """Log a single LLM call"""
        
        timestamp = datetime.now()
        total_tokens = input_tokens + output_tokens
        
        # Calculate cost (OpenAI pricing)
        cost = self._calculate_cost(model, input_tokens, output_tokens)
        
        # Create log entry
        log_entry = {
            'timestamp': timestamp.isoformat(),
            'run_id': self.run_id,
            'app': self.app,
            'test_case_id': test_case_id,
            'step_no': step_no,
            'reason': reason,  # Why LLM was called
            'model': model,
            'input_tokens': input_tokens,
            'output_tokens': output_tokens,
            'total_tokens': total_tokens,
            'cost_usd': cost,
            'latency_ms': latency_ms,
            'status': status
        }
        
        # Store in memory
        self.calls.append(log_entry)
        self.total_tokens += total_tokens
        self.total_cost += cost
        self.call_count += 1
        
        # Track by model
        if model not in self.model_usage:
            self.model_usage[model] = {
                'calls': 0,
                'tokens': 0,
                'cost': 0.0
            }
        self.model_usage[model]['calls'] += 1
        self.model_usage[model]['tokens'] += total_tokens
        self.model_usage[model]['cost'] += cost
        
        # Write to JSONL file
        self._write_to_jsonl(log_entry)
    
    def _calculate_cost(self, model: str, input_tokens: int, output_tokens: int) -> float:
        """Calculate cost based on model pricing"""
        
        # OpenAI pricing (October 2026)
        pricing = {
            'gpt-3.5-turbo': {'input': 0.50, 'output': 1.50},  # per 1M tokens
            'gpt-4': {'input': 30.00, 'output': 60.00},
            'gpt-4-turbo': {'input': 10.00, 'output': 30.00},
            'gpt-4o': {'input': 5.00, 'output': 15.00},
        }
        
        if model not in pricing:
            return 0.0
        
        rates = pricing[model]
        input_cost = (input_tokens / 1_000_000) * rates['input']
        output_cost = (output_tokens / 1_000_000) * rates['output']
        
        return round(input_cost + output_cost, 6)
    
    def _write_to_jsonl(self, entry: Dict):
        """Append entry to JSONL file"""
        log_file = self.log_dir / "llm_calls.jsonl"
        with open(log_file, "a") as f:
            f.write(json.dumps(entry) + "\n")
    
    def log_run_summary(self, suite_name: str, metrics: Dict):
        """Log run summary"""
        
        summary = {
            'timestamp': datetime.now().isoformat(),
            'run_id': self.run_id,
            'app': self.app,
            'suite_name': suite_name,
            'total_tokens': self.total_tokens,
            'total_cost_usd': self.total_cost,
            'llm_calls': self.call_count,
            'model_usage': self.model_usage,
            'cache_hits': metrics.get('cache_hits', 0),
            'cache_hit_rate': metrics.get('cache_hit_rate', 0),
            'duration_seconds': metrics.get('duration_seconds', 0),
        }
        
        # Write to JSONL
        run_log_file = self.log_dir / "run_history.jsonl"
        with open(run_log_file, "a") as f:
            f.write(json.dumps(summary) + "\n")
        
        return summary
    
    def get_summary(self) -> Dict:
        """Get current run summary"""
        return {
            'run_id': self.run_id,
            'total_tokens': self.total_tokens,
            'total_cost': self.total_cost,
            'call_count': self.call_count,
            'model_usage': self.model_usage,
            'calls': self.calls
        }
    
    def print_summary(self):
        """Print summary to console"""
        print("\n" + "="*60)
        print("LLM USAGE SUMMARY")
        print("="*60)
        print(f"\nTotal API Calls: {self.call_count}")
        print(f"Total Tokens: {self.total_tokens:,}")
        print(f"Total Cost: ${self.total_cost:.6f}")
        
        if self.model_usage:
            print("\n--- By Model ---")
            for model, stats in self.model_usage.items():
                print(f"{model}:")
                print(f"  Calls: {stats['calls']}")
                print(f"  Tokens: {stats['tokens']:,}")
                print(f"  Cost: ${stats['cost']:.6f}")
        
        print("\n" + "="*60)
