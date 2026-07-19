"""Fake LSP MCP server for testing rockycode's LSP integration.

Exposes: diagnostics, definition, references, hover, workspace_symbol,
document_symbol — with mock data so the agent tools work end-to-end
without a real language server.
"""
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("rockycode-fake-lsp")

_DIAGNOSTICS = """\
  src/models.py:12  WARNING  Unused import 'os' (F401)
  src/models.py:45  ERROR    'UserModel' has no attribute 'emial' — did you mean 'email'?
  src/api/views.py:128  WARNING  Function 'handle_request' too complex (C901)"""

_REFERENCES = """\
5 references to User:

  src/models.py:42        class User(BaseModel):
  src/api/views.py:128    user = User.objects.get(id=pk)
  src/api/views.py:203    name = user.get_full_name()
  src/services/auth.py:56  return self.user.check_password(pw)
  tests/test_user.py:89   assert user.is_active"""

_DEFINITION = """\
definition of User:

  src/models.py:42-60
  class User(BaseModel):
      id: int
      email: str
      name: str
      is_active: bool = True
      def get_full_name(self) -> str: ...
      def check_password(self, pw: str) -> bool: ..."""

_HOVER = """\
type signature of User.get_full_name:

  (self) -> str
  Returns the user's full name, combining first_name and last_name."""

_FILE_SYMBOLS = """\
symbols in src/models.py:

  class User(BaseModel)              line 42
    method get_full_name() -> str    line 56
    method check_password(pw) -> bool line 62
  class UserManager                  line 78
    method create_user(data) -> User line 85"""

_WORKSPACE_SYMBOLS = """\
symbols matching 'User':

  class User               src/models.py:42
  class UserManager         src/models.py:78
  function handle_user      src/api/views.py:120
  class UserSerializer      src/api/serializers.py:15
  class TestUserModel       tests/test_user.py:10"""


@mcp.tool()
def diagnostics(path: str = "") -> str:
    """Get diagnostics for a file or project-wide."""
    if path and "ok" in path.lower():
        return "[no diagnostics]"
    return _DIAGNOSTICS


@mcp.tool()
def definition(symbol: str) -> str:
    """Get the definition of a symbol."""
    return _DEFINITION


@mcp.tool()
def references(symbol: str) -> str:
    """Find all references to a symbol."""
    return _REFERENCES


@mcp.tool()
def hover(symbol: str) -> str:
    """Get hover info for a symbol."""
    return _HOVER


@mcp.tool()
def document_symbol(path: str) -> str:
    """List all symbols in a file."""
    return _FILE_SYMBOLS


@mcp.tool()
def workspace_symbol(query: str) -> str:
    """Search for symbols across the workspace."""
    return _WORKSPACE_SYMBOLS


if __name__ == "__main__":
    mcp.run()
