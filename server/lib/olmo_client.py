"""
OLMo 3 client for text generation and summarization.
"""
import os
from typing import Optional
import torch
from transformers import AutoModelForCausalLM, AutoTokenizer


class OLMoClient:
    """Client for interacting with OLMo 3 models."""
    
    def __init__(
        self,
        model_name: Optional[str] = None,
        device: Optional[str] = None,
        max_length: int = 2048,
    ):
        self.model_name = model_name or os.getenv(
            "OLMO_MODEL_NAME", 
            "allenai/OLMo-2-1124-13B-Instruct"
        )
        self.device = device or os.getenv("OLMO_DEVICE", "cuda" if torch.cuda.is_available() else "cpu")
        self.max_length = max_length
        
        # Load model and tokenizer
        print(f"Loading OLMo model: {self.model_name}")
        self.tokenizer = AutoTokenizer.from_pretrained(self.model_name)
        self.model = AutoModelForCausalLM.from_pretrained(
            self.model_name,
            torch_dtype=torch.float16 if self.device == "cuda" else torch.float32,
            device_map="auto" if self.device == "cuda" else None,
        )
        
        if self.device == "cpu":
            self.model = self.model.to(self.device)
    
    def generate(
        self,
        prompt: str,
        max_new_tokens: int = 512,
        temperature: float = 0.7,
        top_p: float = 0.9,
    ) -> str:
        """
        Generate text from a prompt using OLMo 3.
        
        Args:
            prompt: Input prompt
            max_new_tokens: Maximum number of tokens to generate
            temperature: Sampling temperature
            top_p: Nucleus sampling parameter
            
        Returns:
            Generated text
        """
        # Format prompt for instruction-tuned model
        formatted_prompt = f"<|user|>\n{prompt}\n<|assistant|>\n"
        
        # Tokenize
        inputs = self.tokenizer(
            formatted_prompt,
            return_tensors="pt",
            truncation=True,
            max_length=self.max_length - max_new_tokens,
        ).to(self.device)
        
        # Generate
        with torch.no_grad():
            outputs = self.model.generate(
                **inputs,
                max_new_tokens=max_new_tokens,
                temperature=temperature,
                top_p=top_p,
                do_sample=True,
                pad_token_id=self.tokenizer.eos_token_id,
            )
        
        # Decode and return only the generated portion
        full_text = self.tokenizer.decode(outputs[0], skip_special_tokens=True)
        
        # Extract only the assistant's response
        if "<|assistant|>" in full_text:
            response = full_text.split("<|assistant|>")[-1].strip()
        else:
            response = full_text[len(formatted_prompt):].strip()
        
        return response
    
    def summarize(
        self,
        text: str,
        style: str = "concise",
        max_tokens: int = 256,
    ) -> dict:
        """
        Summarize text using OLMo 3.
        
        Args:
            text: Text to summarize
            style: Summarization style (e.g., 'concise', 'detailed')
            max_tokens: Maximum tokens for summary
            
        Returns:
            Dictionary with 'headline' and 'body' keys
        """
        # Create summarization prompt
        if style == "concise":
            prompt = f"""Please provide a concise summary of the following legislative text. 
First, create a brief headline (under 10 words), then provide a 2-3 sentence summary.

Text to summarize:
{text[:4000]}  # Truncate if too long

Format your response as:
HEADLINE: [your headline here]
SUMMARY: [your 2-3 sentence summary here]"""
        else:
            prompt = f"""Please summarize the following legislative text.
First, create a headline, then provide a detailed summary.

Text to summarize:
{text[:4000]}

Format your response as:
HEADLINE: [your headline here]
SUMMARY: [your detailed summary here]"""
        
        # Generate summary
        response = self.generate(prompt, max_new_tokens=max_tokens)
        
        # Parse response
        headline = ""
        body = ""
        
        if "HEADLINE:" in response and "SUMMARY:" in response:
            parts = response.split("SUMMARY:")
            headline = parts[0].replace("HEADLINE:", "").strip()
            body = parts[1].strip()
        else:
            # Fallback: treat entire response as body
            body = response
            # Try to extract first sentence as headline
            sentences = body.split(".")
            if sentences:
                headline = sentences[0].strip()
        
        return {
            "headline": headline,
            "body": body,
        }


# Global instance (lazy loaded)
_olmo_client = None


def get_olmo_client() -> OLMoClient:
    """Get or create the global OLMo client instance."""
    global _olmo_client
    if _olmo_client is None:
        _olmo_client = OLMoClient()
    return _olmo_client