"""
OpenAI LLM Client for Self-Healing Test Automation Framework

Uses OpenAI API (ChatGPT) for element identification instead of Anthropic Claude.
Supports: gpt-3.5-turbo, gpt-4, gpt-4-turbo, gpt-4o
"""

from openai import OpenAI
from typing import Dict, Any, Optional
import json
import time
import logging

logger = logging.getLogger(__name__)


class OpenAILLMClient:
    """OpenAI API client for element identification"""
    
    def __init__(self, api_key: str, model: str = "gpt-3.5-turbo"):
        """
        Initialize OpenAI client
        
        Args:
            api_key: OpenAI API key
            model: Model to use (gpt-3.5-turbo, gpt-4, gpt-4-turbo, gpt-4o)
        """
        if not api_key:
            raise ValueError("OpenAI API key is required")
        
        self.client = OpenAI(api_key=api_key)
        self.model = model
        self.max_retries = 3
        self.timeout = 30
        
        logger.info(f"OpenAI client initialized with model: {model}")
    
    def identify_element(
        self,
        page_tree: str,
        target: str,
        confidence_threshold: float = 0.85
    ) -> Dict[str, Any]:
        """
        Identify element on page using OpenAI
        
        Args:
            page_tree: Accessibility tree or filtered DOM of page
            target: Description of element to find (e.g., "Login button")
            confidence_threshold: Minimum confidence required
        
        Returns:
            {
                "found": bool,
                "locator": str,  # CSS selector or xpath
                "confidence": float,  # 0.0-1.0
                "reasoning": str,
                "input_tokens": int,
                "output_tokens": int,
                "total_tokens": int,
                "model": str,
                "cost_usd": float
            }
        """
        
        prompt = self._build_prompt(page_tree, target)
        
        for attempt in range(self.max_retries):
            try:
                start_time = time.time()
                
                response = self.client.chat.completions.create(
                    model=self.model,
                    max_tokens=500,
                    messages=[{
                        "role": "user",
                        "content": prompt
                    }],
                    temperature=0.2  # Low temperature for consistency
                )
                
                latency_ms = int((time.time() - start_time) * 1000)
                
                # Parse response
                result_text = response.choices[0].message.content
                result = self._parse_response(result_text)
                
                # Add metadata
                result['input_tokens'] = response.usage.prompt_tokens
                result['output_tokens'] = response.usage.completion_tokens
                result['total_tokens'] = (
                    response.usage.prompt_tokens +
                    response.usage.completion_tokens
                )
                result['model'] = self.model
                result['latency_ms'] = latency_ms
                
                # Calculate cost
                from selfheal.llm.openai_pricing import OpenAIPricing
                result['cost_usd'] = OpenAIPricing.calculate_cost(
                    self.model,
                    result['input_tokens'],
                    result['output_tokens']
                )
                
                logger.info(
                    f"Element identified: {target} "
                    f"(confidence: {result.get('confidence', 0):.2f}, "
                    f"tokens: {result['total_tokens']}, "
                    f"cost: ${result['cost_usd']:.6f})"
                )
                
                return result
                
            except json.JSONDecodeError as e:
                logger.warning(f"Failed to parse JSON response (attempt {attempt + 1}): {e}")
                if attempt < self.max_retries - 1:
                    time.sleep(1)
                    continue
                raise ValueError("Could not parse LLM response as JSON")
            
            except Exception as e:
                logger.warning(f"LLM call failed (attempt {attempt + 1}): {e}")
                if attempt < self.max_retries - 1:
                    time.sleep(2 ** attempt)  # Exponential backoff
                    continue
                raise
        
        raise RuntimeError(f"Failed to identify element after {self.max_retries} retries")
    
    def _build_prompt(self, page_tree: str, target: str) -> str:
        """Build the prompt for element identification"""
        
        return f"""You are a web automation expert. Analyze the page structure and identify the element matching this description.

TARGET ELEMENT: {target}

PAGE STRUCTURE:
{page_tree}

TASK: Find the element that matches the target description.

RESPONSE FORMAT: Return ONLY valid JSON with NO markdown or extra text:
{{
    "found": true or false,
    "locator": "CSS selector, XPath, or other locator string",
    "confidence": 0.0 to 1.0 (how confident you are),
    "reasoning": "Why you chose this element",
    "alternatives": ["alternative selector 1", "alternative selector 2"]
}}

IMPORTANT:
- Return valid JSON only
- confidence must be a number between 0 and 1
- If you cannot find the element, set found to false and locator to null
- Prefer CSS selectors over XPath when possible
- Include multiple selector alternatives if available
- Be conservative: only mark as found if confident >= 0.85"""
    
    def _parse_response(self, response_text: str) -> Dict[str, Any]:
        """Parse LLM response"""
        
        # Try to extract JSON from response
        try:
            # First, try direct parsing
            result = json.loads(response_text)
        except json.JSONDecodeError:
            # Try to find JSON in the response
            import re
            json_match = re.search(r'\{.*\}', response_text, re.DOTALL)
            if json_match:
                result = json.loads(json_match.group())
            else:
                raise ValueError("No JSON found in response")
        
        # Validate response structure
        if "found" not in result:
            result["found"] = False
        if "confidence" not in result:
            result["confidence"] = 0.0
        if "reasoning" not in result:
            result["reasoning"] = "No reasoning provided"
        if "locator" not in result:
            result["locator"] = None
        
        # Ensure confidence is float between 0-1
        if isinstance(result["confidence"], (int, float)):
            result["confidence"] = min(1.0, max(0.0, float(result["confidence"])))
        else:
            result["confidence"] = 0.0
        
        return result
    
    def heal_element(
        self,
        page_tree: str,
        target: str,
        old_locator: str,
        old_fingerprint: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Heal a broken element (find new location)
        
        Args:
            page_tree: Current page structure
            target: Element description
            old_locator: Previous locator that no longer works
            old_fingerprint: Previous element fingerprint
        
        Returns: Same as identify_element
        """
        
        prompt = f"""You are a web automation expert. An element has moved or changed on the page.

TARGET ELEMENT: {target}
OLD LOCATOR: {old_locator}
OLD FINGERPRINT: {json.dumps(old_fingerprint, indent=2)}

CURRENT PAGE STRUCTURE:
{page_tree}

TASK: Find where the element is now. The element might have moved, changed ID, or been redesigned.

RESPONSE FORMAT: Return ONLY valid JSON:
{{
    "found": true or false,
    "locator": "new locator or null",
    "confidence": 0.0 to 1.0,
    "reasoning": "Why you think this is the same element",
    "alternatives": []
}}"""
        
        # Reuse identify_element logic but with healing prompt
        # For now, just call identify_element (in production, use custom prompt above)
        return self.identify_element(page_tree, target)


def create_llm_client(api_key: str, model: str = "gpt-3.5-turbo") -> OpenAILLMClient:
    """Factory function to create OpenAI client"""
    return OpenAILLMClient(api_key, model)
