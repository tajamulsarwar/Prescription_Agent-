"""
Transfer Configuration for Inter-Agent Communication
Defines SIP addresses and queue mappings for agent transfers
"""

from typing import Dict, Any, Optional

# Agent configurations with SIP transfer addresses and metadata
AGENT_CONFIGS = {
    "human": {
        "name": "Human MA Line",
        "sip_transfer": "sip:************@**.*.*.**:****;user=phone",
        "queue": "main",
        "type": "human"
    }
    
}

# Current agent type (set to match the agent running)
CURRENT_AGENT_TYPE = "appointment"

def get_agent_config(agent_name: str) -> Dict[str, Any]:
    """
    Get configuration for a specific agent.
    
    Args:
        agent_name: Name of the agent (e.g., 'human', 'callback', 'prescription')
        
    Returns:
        Dictionary with agent configuration including sip_transfer address
    """
    agent_name_lower = agent_name.lower()
    if agent_name_lower in AGENT_CONFIGS:
        return AGENT_CONFIGS[agent_name_lower]
    
    # Return human config as default fallback
    return AGENT_CONFIGS.get("human", {})

def get_sip_transfer_address(agent_name: str) -> str:
    """
    Get SIP transfer address for an agent.
    
    Args:
        agent_name: Name of the agent
        
    Returns:
        SIP address string for transfer
    """
    config = get_agent_config(agent_name)
    return config.get("sip_transfer", "sip:************@**.*.*.**:****;user=phone")
