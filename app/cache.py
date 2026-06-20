import json
import time
import hashlib
import redis
import numpy as np
from typing import Callable, Any, Optional
from functools import wraps
from app.config import settings
from app.memory import get_embedding

# Connect to Redis with fail-open capability
redis_client = None
try:
    redis_client = redis.Redis(
        host=settings.REDIS_HOST,
        port=settings.REDIS_PORT,
        db=1,  # Use DB 1 for semantic cache, keeping DB 0 for Celery
        socket_timeout=2.0,
        decode_responses=True
    )
    redis_client.ping()
except Exception as e:
    print(f"Warning: Redis cache connection failed: {e}. Semantic cache will be disabled.")
    redis_client = None

def check_semantic_cache(prompt: str) -> Optional[str]:
    """
    Checks if a prompt with >0.95 cosine similarity exists in the Redis cache.
    Returns the cached response string if found, otherwise None.
    """
    if not redis_client:
        return None

    try:
        # Get all cache keys
        keys = redis_client.keys("scache:*")
        if not keys:
            return None

        # Compute query embedding
        query_emb = np.array(get_embedding(prompt))
        query_norm = np.linalg.norm(query_emb)
        if query_norm == 0:
            return None

        # Scan cache keys and compute similarity
        for key in keys:
            try:
                cached_data_str = redis_client.get(key)
                if not cached_data_str:
                    continue
                
                cached_data = json.loads(cached_data_str)
                cached_emb = np.array(cached_data["embedding"])
                cached_norm = np.linalg.norm(cached_emb)
                
                if cached_norm == 0:
                    continue

                # Cosine similarity
                similarity = float(np.dot(query_emb, cached_emb) / (query_norm * cached_norm))
                
                if similarity > 0.95:
                    print(f"Semantic Cache Hit! Similarity: {similarity:.4f} for key: {key}")
                    return cached_data["response"]
            except Exception as inner_ex:
                print(f"Error parsing cache key {key}: {inner_ex}")
                continue

    except Exception as e:
        print(f"Error checking semantic cache: {e}")
        
    return None

def set_semantic_cache(prompt: str, response: str):
    """
    Saves a prompt, its embedding, and the response to Redis.
    """
    if not redis_client:
        return

    try:
        embedding = get_embedding(prompt)
        cache_data = {
            "prompt": prompt,
            "embedding": embedding,
            "response": response
        }
        # Generate a unique key based on the prompt's hash
        prompt_hash = hashlib.sha256(prompt.encode('utf-8')).hexdigest()
        key = f"scache:{prompt_hash}"
        redis_client.setex(key, 86400, json.dumps(cache_data))  # Expire cache in 24 hours
    except Exception as e:
        print(f"Error setting semantic cache: {e}")

def circuit_breaker(max_retries: int = 5, initial_backoff: float = 1.0):
    """
    Decorator to pause celery task execution and backoff exponentially
    if rate limit (e.g. 429) errors or connection errors occur.
    """
    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        @wraps(func)
        def wrapper(*args, **kwargs) -> Any:
            backoff = initial_backoff
            for attempt in range(max_retries):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    err_msg = str(e).lower()
                    # Check if error is related to rate limit (429) or resource exhausted
                    is_rate_limit = ("429" in err_msg or 
                                     "rate limit" in err_msg or 
                                     "resource exhausted" in err_msg or 
                                     "quota" in err_msg)
                    
                    if is_rate_limit and attempt < max_retries - 1:
                        sleep_time = backoff * (2 ** attempt)
                        print(f"Rate limit hit during {func.__name__}. Retrying in {sleep_time:.2f}s... (Attempt {attempt+1}/{max_retries})")
                        time.sleep(sleep_time)
                    else:
                        # Re-raise the exception if not a rate limit error or last attempt reached
                        raise e
            return func(*args, **kwargs)
        return wrapper
    return decorator
