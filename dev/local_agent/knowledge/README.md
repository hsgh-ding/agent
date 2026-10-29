# RTL Agent Knowledge Base

This directory is an offline, human-editable knowledge base. Each Markdown file
is a topic, and the agent performs lightweight keyword retrieval before a model
request. It does not send the whole knowledge base to the model.

Add project-specific rules, interfaces, and proven design patterns as separate
`.md` files. Keep entries factual and concise. The runtime treats knowledge as
reference data, not executable instructions.
