# 4. One MCP server per company system, writes for code only

**Status:** accepted

## Context
A real accounts-payable process touches several systems: the document inbox, the ERP, email and Teams.

## Decision
Each system is its own MCP server (`documents`, `erp`, `mail`, `notify`). The model may call only the allow-listed
read tools; write tools are called by code. Message recipients are directory keys (`role:ap_manager`), never
addresses, so invoice text cannot redirect a message.

## Consequences
- Adding a system (ServiceNow, SAP) means adding a server, not changing the agent.
- The same servers run as child processes in the demo and as separate services in Docker and AWS.
