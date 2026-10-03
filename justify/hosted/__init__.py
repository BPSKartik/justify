"""
Justify as a public service: scan any public GitHub repository by URL, from a web page,
a JSON API, or an AI assistant over MCP.

The hosted service only ever reads code. It clones, parses and blames; it never runs the
repository's tests or any of its code, and it never calls a model on a stranger's behalf.
Proof and model judgement stay on the user's own machine (`pip install "justify-code[mcp]"`).
"""
