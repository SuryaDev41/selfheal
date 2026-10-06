"""3-Tier Element Resolver with Caching - FIXED V2"""

import os
from pathlib import Path
from dotenv import load_dotenv
from .cache import CacheResolver

# Load environment variables
load_dotenv(".env.openai")
load_dotenv(".env")

class ElementResolver:
    """
    3-Tier Element Resolution:
    Tier 1: Check database cache (0 tokens, instant) ✅ CACHE HIT
    Tier 2: Try fallback patterns (0 tokens, instant) ✅ CACHE & SAVE
    Tier 3: Call LLM (tokens, 1-2 seconds) ✅ CACHE & SAVE
    """
    
    def __init__(self, app: str = 'automationexercise', env: str = 'prod'):
        self.app = app
        self.env = env
        self.cache = CacheResolver()
        self.cache_hits = 0
        self.llm_calls = 0
        self.cached_elements = 0
        
        # Get OpenAI client directly (not through config_loader)
        try:
            from openai import OpenAI
            api_key = os.getenv("OPENAI_API_KEY")
            if not api_key:
                raise ValueError("OPENAI_API_KEY not set")
            self.client = OpenAI(api_key=api_key)
            self.model = os.getenv("OPENAI_MODEL", "gpt-3.5-turbo")
            print(f"✅ OpenAI client initialized with model: {self.model}")
        except Exception as e:
            print(f"❌ Failed to initialize OpenAI: {e}")
            self.client = None
    
    def resolve_element(self, page: str, target: str, context: str = ""):
        """
        Resolve element using 3-tier approach
        
        Args:
            page: Page name (e.g., 'login', 'products')
            target: Element target (e.g., 'Login Button')
            context: Additional context for LLM
            
        Returns:
            dict with locator and source (cache/pattern/llm)
        """
        
        # ===== TIER 1: Check Database Cache =====
        cached = self.cache.get_cached_element(self.app, self.env, page, target)
        if cached:
            self.cache_hits += 1
            print(f"  ✅ {target}: FROM CACHE (0 tokens, $0)")
            return {
                'locator': cached,
                'source': 'cache',
                'tokens': 0,
                'cost': 0.0,
                'latency_ms': 10
            }
        
        # ===== TIER 2: Try Fallback Patterns =====
        pattern_result = self._try_fallback_patterns(page, target)
        if pattern_result:
            print(f"  ⚡ {target}: Pattern match")
            
            # IMPORTANT: Cache the pattern for next time!
            self.cache.cache_element(
                app=self.app,
                env=self.env,
                page=page,
                target=target,
                locator=pattern_result
            )
            self.cached_elements += 1
            
            return {
                'locator': pattern_result,
                'source': 'pattern',
                'tokens': 0,
                'cost': 0.0,
                'latency_ms': 5
            }
        
        # ===== TIER 3: Call LLM =====
        if not self.client:
            print(f"  ❌ No LLM client available for {target}")
            return None
        
        print(f"  🔄 Calling LLM for {target}...")
        llm_result = self._call_llm(page, target, context)
        
        if llm_result:
            self.llm_calls += 1
            # Cache the result for next time
            self.cache.cache_element(
                app=self.app,
                env=self.env,
                page=page,
                target=target,
                locator=llm_result['locator']
            )
            self.cached_elements += 1
            
            return llm_result
        
        return None
    
    def _try_fallback_patterns(self, page: str, target: str) -> str:
        """Try common XPath/CSS patterns"""
        
        patterns = {
            'login': {
                'Login Button': "//button[contains(text(), 'Login')]",
                'Email': "//input[@name='email']",
                'Password': "//input[@name='password']",
            },
            'register': {
                'Sign Up Button': "//button[contains(text(), 'Sign Up')]",
                'First Name': "//input[@name='firstName']",
            },
            'products': {
                'Add to Cart': "//button[contains(text(), 'Add')]",
                'Product Name': "//h2[@class='product-name']",
            },
            'cart': {
                'Checkout Button': "//button[contains(text(), 'Checkout')]",
                'Total': "//span[@class='total-price']",
            },
            'profile': {
                'Edit Profile': "//button[contains(text(), 'Edit')]",
                'Save Button': "//button[@type='submit']",
            },
        }
        
        if page in patterns and target in patterns[page]:
            return patterns[page][target]
        
        return None
    
    def _call_llm(self, page: str, target: str, context: str) -> dict:
        """Call OpenAI to identify element"""
        
        prompt = f"""
You are a test automation expert. Identify the XPath locator for an element.

Page: {page}
Element Target: {target}
Context: {context}

Return ONLY a valid XPath locator. No explanation. Example: //button[@id='submit']
"""
        
        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "user", "content": prompt}
                ],
                temperature=0.3,
                max_tokens=100
            )
            
            locator = response.choices[0].message.content.strip()
            
            # Calculate cost (gpt-3.5-turbo pricing)
            input_tokens = len(prompt.split())
            output_tokens = len(locator.split())
            total_tokens = input_tokens + output_tokens
            
            # gpt-3.5-turbo: $0.0005 per 1K input, $0.0015 per 1K output
            cost = (input_tokens * 0.0005 + output_tokens * 0.0015) / 1000
            
            print(f"    LLM Response: {locator}")
            print(f"    Tokens: {total_tokens}, Cost: ${cost:.6f}")
            
            return {
                'locator': locator,
                'source': 'llm',
                'tokens': total_tokens,
                'cost': cost,
                'latency_ms': 2000
            }
        
        except Exception as e:
            print(f"    ❌ LLM Error: {e}")
            return None
    
    def get_stats(self) -> dict:
        """Get resolver statistics"""
        return {
            'cache_hits': self.cache_hits,
            'llm_calls': self.llm_calls,
            'cached_elements': self.cached_elements,
            'total_elements_resolved': self.cache_hits + self.llm_calls
        }