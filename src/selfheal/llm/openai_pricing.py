"""
OpenAI Pricing Calculator for Self-Healing Test Automation Framework

Pricing updated to October 2026
Supports: GPT-3.5 Turbo, GPT-4, GPT-4 Turbo, GPT-4o
"""

from typing import Dict, Tuple


class OpenAIPricing:
    """OpenAI API pricing (October 2026)"""
    
    # Model pricing per 1M tokens (input / output)
    MODELS = {
        'gpt-3.5-turbo': {
            'input': 0.50,      # $0.50 per 1M input tokens
            'output': 1.50,     # $1.50 per 1M output tokens
            'name': 'GPT-3.5 Turbo',
            'recommended': True,  # Best value for this project
            'description': 'Fast and cheap'
        },
        'gpt-4': {
            'input': 30.00,     # $30 per 1M input tokens
            'output': 60.00,    # $60 per 1M output tokens
            'name': 'GPT-4',
            'recommended': False,
            'description': 'Powerful but expensive'
        },
        'gpt-4-turbo': {
            'input': 10.00,     # $10 per 1M input tokens
            'output': 30.00,    # $30 per 1M output tokens
            'name': 'GPT-4 Turbo',
            'recommended': False,
            'description': 'More capable but more expensive'
        },
        'gpt-4o': {
            'input': 5.00,      # $5 per 1M input tokens
            'output': 15.00,    # $15 per 1M output tokens
            'name': 'GPT-4o',
            'recommended': False,
            'description': 'Latest model, balanced'
        }
    }
    
    @staticmethod
    def calculate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
        """
        Calculate cost in USD for API call
        
        Args:
            model: Model name
            input_tokens: Number of input tokens
            output_tokens: Number of output tokens
        
        Returns:
            Cost in USD (rounded to 6 decimals)
        """
        
        if model not in OpenAIPricing.MODELS:
            return 0.0
        
        pricing = OpenAIPricing.MODELS[model]
        
        input_cost = (input_tokens / 1_000_000) * pricing['input']
        output_cost = (output_tokens / 1_000_000) * pricing['output']
        
        return round(input_cost + output_cost, 6)
    
    @staticmethod
    def get_model_name(model: str) -> str:
        """Get friendly model name"""
        if model in OpenAIPricing.MODELS:
            return OpenAIPricing.MODELS[model]['name']
        return model
    
    @staticmethod
    def is_recommended(model: str) -> bool:
        """Check if model is recommended for this project"""
        if model in OpenAIPricing.MODELS:
            return OpenAIPricing.MODELS[model].get('recommended', False)
        return False
    
    @staticmethod
    def get_description(model: str) -> str:
        """Get model description"""
        if model in OpenAIPricing.MODELS:
            return OpenAIPricing.MODELS[model].get('description', '')
        return 'Unknown model'
    
    @staticmethod
    def estimate_tokens(text: str) -> int:
        """
        Rough estimate of tokens (OpenAI tokenizer)
        
        GPT models use different tokenization:
        - 1 token ≈ 4 characters
        - More accurate: use tiktoken library
        
        This is a conservative estimate.
        """
        return len(text) // 4
    
    @staticmethod
    def estimate_run_cost(
        total_tokens: int,
        model: str = 'gpt-3.5-turbo'
    ) -> float:
        """
        Estimate cost for a run
        
        Args:
            total_tokens: Total tokens used
            model: Model name
        
        Returns:
            Estimated cost in USD
        """
        
        # Assume 70% input, 30% output tokens (typical ratio)
        input_tokens = int(total_tokens * 0.7)
        output_tokens = int(total_tokens * 0.3)
        
        return OpenAIPricing.calculate_cost(model, input_tokens, output_tokens)
    
    @staticmethod
    def compare_models() -> Dict[str, Dict]:
        """Get comparison of all available models"""
        return {
            model: {
                'name': info['name'],
                'input_price': info['input'],
                'output_price': info['output'],
                'recommended': info.get('recommended', False),
                'description': info.get('description', '')
            }
            for model, info in OpenAIPricing.MODELS.items()
        }
    
    @staticmethod
    def get_recommended_model() -> str:
        """Get recommended model for this project"""
        for model, info in OpenAIPricing.MODELS.items():
            if info.get('recommended', False):
                return model
        return 'gpt-3.5-turbo'  # Fallback


def compare_costs(
    input_tokens: int,
    output_tokens: int
) -> Dict[str, float]:
    """
    Compare costs across all models for given tokens
    
    Args:
        input_tokens: Number of input tokens
        output_tokens: Number of output tokens
    
    Returns:
        Dictionary of model -> cost
    """
    
    costs = {}
    for model in OpenAIPricing.MODELS.keys():
        costs[model] = OpenAIPricing.calculate_cost(
            model,
            input_tokens,
            output_tokens
        )
    
    return costs


def print_pricing_table():
    """Print pricing comparison table"""
    
    print("\n" + "="*80)
    print("OpenAI Model Pricing Comparison (October 2026)")
    print("="*80)
    print(f"{'Model':<20} {'Input':<15} {'Output':<15} {'Recommended':<15} {'Description':<20}")
    print("-"*80)
    
    for model, info in OpenAIPricing.MODELS.items():
        recommended = "✓ YES" if info.get('recommended', False) else "NO"
        print(
            f"{model:<20} "
            f"${info['input']:<14.2f} "
            f"${info['output']:<14.2f} "
            f"{recommended:<15} "
            f"{info.get('description', ''):<20}"
        )
    
    print("="*80)
    print("\nPricing per 1M tokens (input / output)")
    print("For element identification in this project:")
    print("  - Typical per-element cost: $0.0005 - $0.001")
    print("  - Per 100-step suite (Run 1): $0.01 - $0.05")
    print("  - Per 100-step suite (Run 2+): $0 (cached)")
    print("="*80 + "\n")


if __name__ == "__main__":
    # Print pricing table
    print_pricing_table()
    
    # Example cost calculation
    example_input = 1500
    example_output = 200
    print(f"Example: {example_input} input + {example_output} output tokens")
    print("-" * 40)
    costs = compare_costs(example_input, example_output)
    for model, cost in sorted(costs.items(), key=lambda x: x[1]):
        print(f"  {model:<20}: ${cost:.6f}")
    print()
