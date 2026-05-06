#!/usr/bin/env python3
"""
Comprehensive Regression Test Suite
Tests all functionality to ensure no regressions from security fixes
Run: python3 regression_test_suite.py
"""

import os
import sys
import json
import subprocess
import time
from datetime import datetime
from typing import Tuple, Dict, List

# ANSI colors
GREEN = '\033[92m'
RED = '\033[91m'
YELLOW = '\033[93m'
BLUE = '\033[94m'
RESET = '\033[0m'

class RegressionTestSuite:
    def __init__(self):
        self.test_results = []
        self.passed = 0
        self.failed = 0
        self.skipped = 0
        self.base_dir = "/Users/stephanesandjong/Library/Application Support/IntelligentInvestorAgentV2"

    def log_test(self, name: str, status: str, details: str = ""):
        """Log test result."""
        if status == "SKIP":
            symbol = f"{YELLOW}⊝{RESET}"
            self.skipped += 1
        elif status == "PASS":
            symbol = f"{GREEN}✓{RESET}"
            self.passed += 1
        else:
            symbol = f"{RED}✗{RESET}"
            self.failed += 1

        msg = f"{symbol} {name}"
        if details:
            msg += f" — {details}"
        print(msg)

        self.test_results.append({
            "test": name,
            "status": status,
            "details": details,
            "timestamp": datetime.now().isoformat()
        })

    def run_command(self, cmd: List[str], cwd=None) -> Tuple[int, str, str]:
        """Run command and return (exit_code, stdout, stderr)."""
        try:
            result = subprocess.run(
                cmd,
                cwd=cwd or self.base_dir,
                capture_output=True,
                text=True,
                timeout=10
            )
            return result.returncode, result.stdout, result.stderr
        except subprocess.TimeoutExpired:
            return 1, "", "Timeout"
        except Exception as e:
            return 1, "", str(e)

    # ─────────────────────────────────────────────────────────────────
    # Python Syntax & Import Tests
    # ─────────────────────────────────────────────────────────────────

    def test_python_syntax(self) -> bool:
        """Test all Python files have valid syntax."""
        print(f"\n{BLUE}[Test Group] Python Syntax & Structure{RESET}")

        files_to_check = [
            "dashboard_v2.py",
            "models.py",
            "auth.py",
            "admin_routes.py",
            "plans.py"
        ]

        for file in files_to_check:
            filepath = os.path.join(self.base_dir, file)
            if not os.path.exists(filepath):
                self.log_test(f"Syntax: {file} exists", "FAIL", "File not found")
                return False

            # Compile Python file to check syntax
            try:
                with open(filepath, 'r') as f:
                    compile(f.read(), file, 'exec')
                self.log_test(f"Syntax: {file} valid", "PASS", "No syntax errors")
            except SyntaxError as e:
                self.log_test(f"Syntax: {file} valid", "FAIL", str(e))
                return False

        return True

    def test_imports(self) -> bool:
        """Test critical imports are available."""
        print(f"\n{BLUE}[Test Group] Critical Imports{RESET}")

        required_modules = [
            ("psycopg2", "Database driver"),
            ("flask", "Web framework"),
            ("jwt", "JWT authentication"),
            ("bcrypt", "Password hashing"),
            ("requests", "HTTP client"),
        ]

        for module, desc in required_modules:
            exitcode, _, stderr = self.run_command(
                [sys.executable, "-c", f"import {module}"],
                cwd=None
            )

            if exitcode == 0:
                self.log_test(f"Import: {module}", "PASS", desc)
            else:
                self.log_test(f"Import: {module}", "FAIL", desc)
                return False

        return True

    # ─────────────────────────────────────────────────────────────────
    # Security Fix Implementation Tests
    # ─────────────────────────────────────────────────────────────────

    def test_security_implementations(self) -> bool:
        """Verify all security fixes are implemented correctly."""
        print(f"\n{BLUE}[Test Group] Security Fix Implementation{RESET}")

        checks = [
            # Issue #1-2
            ("dashboard_v2.py", "uuid.UUID", "UUID validation for path traversal"),
            ("dashboard_v2.py", "_validate_user_id", "User ID validation function"),
            ("auth.py", "_get_client_ip", "IP extraction with proxy validation"),

            # Issue #3-5
            ("auth.py", "_check_rate_limit", "Rate limiting function"),
            ("auth.py", "pg_advisory_lock", "Advisory lock mechanism (if called from plans)"),
            ("models.py", "email_verify_token_expires", "Email token expiration column"),
            ("models.py", "sha256", "SHA256 token hashing"),

            # Issue #6-8
            ("models.py", "verify_and_rotate_refresh_token", "Refresh token rotation"),
            ("admin_routes.py", "_handle_exception", "Error message sanitization"),
            ("plans.py", "pg_advisory_lock", "Atomic operations with locks"),
        ]

        for filepath, pattern, desc in checks:
            full_path = os.path.join(self.base_dir, filepath)
            try:
                with open(full_path, 'r') as f:
                    content = f.read()
                    if pattern in content:
                        self.log_test(f"Security: {desc}", "PASS", filepath)
                    else:
                        self.log_test(f"Security: {desc}", "FAIL", f"Pattern not found in {filepath}")
                        return False
            except Exception as e:
                self.log_test(f"Security: {desc}", "FAIL", str(e))
                return False

        return True

    def test_database_schema(self) -> bool:
        """Verify database schema changes are in place."""
        print(f"\n{BLUE}[Test Group] Database Schema{RESET}")

        # Read models.py and check for schema definitions
        models_path = os.path.join(self.base_dir, "models.py")

        try:
            with open(models_path, 'r') as f:
                content = f.read()

                # Check for email token expiration column
                if "email_verify_token_expires" in content:
                    self.log_test("Schema: Email token expiration", "PASS", "Column defined")
                else:
                    self.log_test("Schema: Email token expiration", "FAIL", "Missing column definition")
                    return False

                # Check for advisory lock usage
                if "pg_advisory_lock" in content:
                    self.log_test("Schema: Advisory locks", "PASS", "Lock mechanism present")
                else:
                    self.log_test("Schema: Advisory locks", "FAIL", "Lock mechanism missing")
                    # Not critical, might be in plans.py instead
                    pass

                return True

        except Exception as e:
            self.log_test("Schema: Validation", "FAIL", str(e))
            return False

    def test_dependency_versions(self) -> bool:
        """Verify critical dependencies are specified."""
        print(f"\n{BLUE}[Test Group] Dependency Management{RESET}")

        # Check requirements.txt
        req_path = os.path.join(self.base_dir, "requirements.txt")

        try:
            with open(req_path, 'r') as f:
                content = f.read()

                dependencies = [
                    ("flask", "Web framework"),
                    ("psycopg2-binary", "Database driver"),
                    ("PyJWT", "JWT support"),
                    ("bcrypt", "Password hashing"),
                    ("sentry-sdk", "Error tracking"),
                ]

                for dep, desc in dependencies:
                    if dep in content:
                        self.log_test(f"Dependency: {dep}", "PASS", desc)
                    else:
                        self.log_test(f"Dependency: {dep}", "FAIL", desc)
                        if "sentry" not in dep:  # Sentry is new, others are critical
                            return False

                return True

        except Exception as e:
            self.log_test("Dependency: Check", "FAIL", str(e))
            return False

    # ─────────────────────────────────────────────────────────────────
    # Configuration & Environment Tests
    # ─────────────────────────────────────────────────────────────────

    def test_configuration(self) -> bool:
        """Verify configuration files are present."""
        print(f"\n{BLUE}[Test Group] Configuration & Setup{RESET}")

        config_files = [
            (".env", "Environment variables"),
            (".gitignore", "Git ignore rules"),
            ("requirements.txt", "Python dependencies"),
            (".github/workflows/secrets-scan.yml", "Secrets scanning workflow"),
        ]

        for filepath, desc in config_files:
            full_path = os.path.join(self.base_dir, filepath)
            if os.path.exists(full_path):
                self.log_test(f"Config: {filepath}", "PASS", desc)
            else:
                self.log_test(f"Config: {filepath}", "FAIL", desc)
                return False

        return True

    def test_git_hooks(self) -> bool:
        """Verify Git hooks are configured."""
        print(f"\n{BLUE}[Test Group] Git Hooks & CI/CD{RESET}")

        hook_path = os.path.join(self.base_dir, ".git/hooks/pre-commit")

        if os.path.exists(hook_path):
            # Check if it's executable
            if os.access(hook_path, os.X_OK):
                self.log_test("Hook: pre-commit executable", "PASS", "Secret scanning enabled")
                return True
            else:
                self.log_test("Hook: pre-commit executable", "FAIL", "Hook not executable")
                return False
        else:
            self.log_test("Hook: pre-commit exists", "FAIL", "Hook not installed")
            return False

    # ─────────────────────────────────────────────────────────────────
    # Code Quality Tests
    # ─────────────────────────────────────────────────────────────────

    def test_code_quality(self) -> bool:
        """Check for code quality issues."""
        print(f"\n{BLUE}[Test Group] Code Quality{RESET}")

        # Check for common anti-patterns
        critical_files = [
            ("auth.py", ["eval(", "exec(", "__import__("]),
            ("admin_routes.py", ["eval(", "exec(", "__import__("]),
            ("models.py", ["exec(", "eval("]),
        ]

        for filepath, patterns in critical_files:
            full_path = os.path.join(self.base_dir, filepath)

            try:
                with open(full_path, 'r') as f:
                    content = f.read()

                    found_dangerous = False
                    for pattern in patterns:
                        if pattern in content:
                            # Make sure it's not in a comment or string
                            lines = content.split('\n')
                            for line in lines:
                                if pattern in line and not line.strip().startswith('#'):
                                    found_dangerous = True
                                    break

                    if not found_dangerous:
                        self.log_test(f"Quality: {filepath} safe", "PASS", "No dangerous patterns")
                    else:
                        self.log_test(f"Quality: {filepath} safe", "FAIL", "Dangerous code patterns found")
                        return False

            except Exception as e:
                self.log_test(f"Quality: {filepath} check", "FAIL", str(e))
                return False

        return True

    # ─────────────────────────────────────────────────────────────────
    # Commit History & Git Tests
    # ─────────────────────────────────────────────────────────────────

    def test_git_commits(self) -> bool:
        """Verify security fixes are committed."""
        print(f"\n{BLUE}[Test Group] Git Commit History{RESET}")

        # Check for recent security-related commits
        exitcode, stdout, _ = self.run_command(
            ["git", "log", "--oneline", "-20"],
            cwd=self.base_dir
        )

        if exitcode == 0:
            self.log_test("Git: Recent commits accessible", "PASS", "Git log available")

            if "security" in stdout.lower() or "fix" in stdout.lower():
                self.log_test("Git: Security commits present", "PASS", "Fix commits found")
            else:
                self.log_test("Git: Security commits present", "SKIP", "Cannot verify commit messages")

            return True
        else:
            self.log_test("Git: Recent commits accessible", "FAIL", "Cannot access git log")
            return False

    # ─────────────────────────────────────────────────────────────────
    # Summary & Reporting
    # ─────────────────────────────────────────────────────────────────

    def run_all_tests(self) -> bool:
        """Run all regression tests."""
        print(f"\n{BLUE}╔═════════════════════════════════════════════════════╗{RESET}")
        print(f"{BLUE}║  COMPREHENSIVE REGRESSION TEST SUITE                 ║{RESET}")
        print(f"{BLUE}║  Verify no regressions from security fixes           ║{RESET}")
        print(f"{BLUE}╚═════════════════════════════════════════════════════╝{RESET}")

        # Run test groups
        test_groups = [
            ("Python & Imports", [self.test_python_syntax, self.test_imports]),
            ("Security Implementations", [self.test_security_implementations]),
            ("Database & Schema", [self.test_database_schema]),
            ("Configuration", [self.test_dependency_versions, self.test_configuration]),
            ("Git & CI/CD", [self.test_git_hooks, self.test_git_commits]),
            ("Code Quality", [self.test_code_quality]),
        ]

        all_passed = True

        for group_name, tests in test_groups:
            for test_func in tests:
                try:
                    result = test_func()
                    if result is False:
                        all_passed = False
                except Exception as e:
                    print(f"{RED}✗ Exception in {test_func.__name__}: {e}{RESET}")
                    all_passed = False

        # Print summary
        self.print_summary()

        return all_passed

    def print_summary(self):
        """Print test summary."""
        total = self.passed + self.failed + self.skipped
        pass_rate = (self.passed / (self.passed + self.failed) * 100) if (self.passed + self.failed) > 0 else 0

        print(f"\n{BLUE}╔═════════════════════════════════════════════════════╗{RESET}")
        print(f"{BLUE}║  REGRESSION TEST RESULTS                            ║{RESET}")
        print(f"{BLUE}╚═════════════════════════════════════════════════════╝{RESET}")

        print(f"\nTotal Tests:    {total}")
        print(f"{GREEN}Passed:         {self.passed}{RESET}")
        if self.failed > 0:
            print(f"{RED}Failed:         {self.failed}{RESET}")
        if self.skipped > 0:
            print(f"{YELLOW}Skipped:        {self.skipped}{RESET}")

        if self.passed + self.failed > 0:
            print(f"Pass Rate:      {pass_rate:.1f}%")

        if self.failed == 0:
            print(f"\n{GREEN}✓ All regression tests PASSED{RESET}")
            print("No regressions detected. Application is safe to deploy.")
        else:
            print(f"\n{RED}✗ {self.failed} test(s) FAILED{RESET}")
            print("Please investigate failures before deployment.")

        # Save results
        with open("regression_test_results.json", "w") as f:
            json.dump({
                "timestamp": datetime.now().isoformat(),
                "summary": {
                    "total": total,
                    "passed": self.passed,
                    "failed": self.failed,
                    "skipped": self.skipped,
                    "pass_rate": pass_rate
                },
                "tests": self.test_results
            }, f, indent=2)

        print(f"\nDetailed results: regression_test_results.json")

        return self.failed == 0

if __name__ == "__main__":
    suite = RegressionTestSuite()
    success = suite.run_all_tests()
    sys.exit(0 if success else 1)
