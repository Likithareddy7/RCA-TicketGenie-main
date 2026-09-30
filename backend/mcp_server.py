"""
MCP Server — OSS Remediation Checks
-----------------------------------
Exposes the validation checks as real MCP tools so any MCP client (an agent, an
LLM tool-use loop, Claude Desktop, etc.) can discover and call them.

Design:
  * Each tool is a THIN `@mcp.tool()` wrapper that delegates to `validation_tools`.
    The check logic lives in one place; the core app stays MCP-free + deterministic.
  * Tools return FACTS only — never a resolution or prose.
  * Adding a check for a new fallout type = add one ValidationTool in
    validation_tools.py + one small @mcp.tool() wrapper here.

Run:
    python mcp_server.py            # Streamable HTTP  (http://127.0.0.1:8765/mcp)
    python mcp_server.py --stdio    # stdio (for a local client like Claude Desktop)

Inspect:
    npx @modelcontextprotocol/inspector
"""

import sys

# The MCP Python SDK changed shape at 2.0: FastMCP was renamed MCPServer and moved
# out of mcp.server.fastmcp, and host/port moved off the constructor onto run().
# The @tool decorator is unchanged. requirements.txt does not pin `mcp`, so which
# major is installed depends on when the environment was built — support both
# rather than pinning the SDK backwards.
HOST, PORT = "127.0.0.1", 8765

try:
    from mcp.server.mcpserver import MCPServer as _Server      # mcp >= 2.0
    _MCP2 = True
except ImportError:                                            # mcp 1.x
    from mcp.server.fastmcp import FastMCP as _Server
    _MCP2 = False

import validation_tools

mcp = (_Server("OSS Remediation Checks") if _MCP2
       else _Server("OSS Remediation Checks", host=HOST, port=PORT))


# ── provisioning domain ──────────────────────────────────────────────────

@mcp.tool(name="provisioning.check_active_service")
def check_active_service(location_id: str) -> dict:
    """Check the provisioning/OMS inventory for an existing active service at a
    Location ID, and report whether a new order there would be a duplicate.

    Args:
        location_id: The Location ID to check, e.g. "LOC-482910".

    Returns:
        Facts only: found, active, pending_disconnect, is_duplicate, and the
        service record. Never a resolution.
    """
    return validation_tools.run_tool(
        "provisioning.check_active_service", {"location_id": location_id}
    )


# ── BOSS / OMS domain ────────────────────────────────────────────────────

@mcp.tool(name="oms.check_account_status")
def check_account_status(ban: str) -> dict:
    """Check whether a BAN's account status agrees with the state of its orders.

    Args:
        ban: Billing account number, e.g. "1000342627".

    Returns:
        Facts only: the current status, the status it should be, and why.
    """
    return validation_tools.run_tool("oms.check_account_status", {"ban": ban})


@mcp.tool(name="oms.check_order_status")
def check_order_status(order_ref: str) -> dict:
    """Check whether an order is stuck — staged but not reflected on the account,
    or held by an open blocking task.

    Args:
        order_ref: Order id, e.g. "TN1100045344".

    Returns:
        Facts only: order status, stage, whether it reflects on the account, and
        any blocking task.
    """
    return validation_tools.run_tool("oms.check_order_status", {"order_ref": order_ref})


@mcp.tool(name="oms.check_network_type")
def check_network_type(ban: str) -> dict:
    """Check whether the banner shown on an account matches its provisioned
    network type.

    Args:
        ban: Billing account number, e.g. "314116701".

    Returns:
        Facts only: the current banner, the provisioned network type, and which
        one is wrong.
    """
    return validation_tools.run_tool("oms.check_network_type", {"ban": ban})


# ── routing domain ───────────────────────────────────────────────────────

@mcp.tool(name="routing.check_redirect_queue")
def check_redirect_queue(number: str) -> dict:
    """Check whether a ticket belongs to another team's queue (e.g. Buy Flow) and
    should be reassigned rather than remediated.

    Args:
        number: Incident number to classify, e.g. "INC0448216".

    Returns:
        Facts only: the matched rule, the owning assignment group, and the text
        that matched. Never performs the reassignment.
    """
    return validation_tools.run_tool("routing.check_redirect_queue", {"number": number})


if __name__ == "__main__":
    transport = "stdio" if "--stdio" in sys.argv else "streamable-http"
    print(f"[MCP] Starting 'OSS Remediation Checks' over {transport} ...")
    # On mcp 2.x the bind address is a run() argument; on 1.x it came from the
    # constructor and passing it here would be rejected. stdio binds nothing.
    if _MCP2 and transport == "streamable-http":
        mcp.run(transport=transport, host=HOST, port=PORT)
    else:
        mcp.run(transport=transport)
