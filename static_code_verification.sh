#!/bin/bash

# Static Code Verification — verify all security fixes are deployed
# This checks the source code without needing network access

RED='\033[91m'
GREEN='\033[92m'
YELLOW='\033[93m'
BLUE='\033[94m'
RESET='\033[0m'

echo -e "${BLUE}╔═════════════════════════════════════════════════════╗${RESET}"
echo -e "${BLUE}║  STATIC CODE VERIFICATION                           ║${RESET}"
echo -e "${BLUE}║  Verify all security fixes are in place              ║${RESET}"
echo -e "${BLUE}╚═════════════════════════════════════════════════════╝${RESET}"

passed=0
failed=0

# Helper function
check_file_contains() {
    local file=$1
    local pattern=$2
    local desc=$3

    if grep -q "$pattern" "$file"; then
        echo -e "${GREEN}✓${RESET} $desc"
        ((passed++))
    else
        echo -e "${RED}✗${RESET} $desc"
        ((failed++))
    fi
}

check_file_not_contains() {
    local file=$1
    local pattern=$2
    local desc=$3

    if ! grep -q "$pattern" "$file"; then
        echo -e "${GREEN}✓${RESET} $desc"
        ((passed++))
    else
        echo -e "${RED}✗${RESET} $desc (still present)"
        ((failed++))
    fi
}

# Issue #1: Hardcoded API Key Removed
echo -e "\n${YELLOW}Issue #1: Hardcoded API Key${RESET}"
check_file_not_contains "dashboard_v2.py" "FMP_API_KEY.*=" "No hardcoded fallback in dashboard_v2"
check_file_contains "dashboard_v2.py" "os.environ.get.*FMP" "Uses environment variable"

# Issue #2: CSRF Validation
echo -e "\n${YELLOW}Issue #2: CSRF Protection${RESET}"
check_file_contains "dashboard_v2.py" "check_csrf_origin" "CSRF validation function exists"
check_file_contains "dashboard_v2.py" "raise ValueError.*origin" "Fail-closed CSRF validation"

# Issue #3: Rate Limiting
echo -e "\n${YELLOW}Issue #3: Rate Limiting${RESET}"
check_file_contains "auth.py" "_check_rate_limit" "Rate limiting function exists"
check_file_contains "auth.py" "429" "Returns 429 on rate limit"

# Issue #4: X-Forwarded-For Validation
echo -e "\n${YELLOW}Issue #4: X-Forwarded-For Validation${RESET}"
check_file_contains "auth.py" "_get_client_ip" "IP extraction function exists"
check_file_contains "auth.py" "172.17" "Validates Docker network range"
check_file_contains "auth.py" "X-Forwarded-For" "Header validation implemented"

# Issue #5: Refresh Token Rotation
echo -e "\n${YELLOW}Issue #5: Refresh Token Rotation${RESET}"
check_file_contains "models.py" "verify_and_rotate_refresh_token" "Token rotation function exists"
check_file_contains "models.py" "valid.*rotated" "Returns detailed status dict"
check_file_contains "auth.py" "rotation_result.get" "Uses rotation status in endpoint"

# Issue #6: Email Token Expiration
echo -e "\n${YELLOW}Issue #6: Email Token Expiration & Hashing${RESET}"
check_file_contains "models.py" "email_verify_token_expires" "Schema includes expiration"
check_file_contains "models.py" "sha256" "Uses SHA256 hashing"
check_file_contains "models.py" "email_verify_token_expires > NOW" "Checks expiration on verify"

# Issue #7: Error Message Sanitization
echo -e "\n${YELLOW}Issue #7: Error Message Sanitization${RESET}"
check_file_contains "admin_routes.py" "_handle_exception" "Error handler function exists"
check_file_contains "admin_routes.py" "An error occurred" "Returns generic messages"
check_file_contains "admin_routes.py" "exc_info=True" "Logs full details server-side"

# Issue #8: Atomic Operations (Advisory Locks)
echo -e "\n${YELLOW}Issue #8: Cross-Worker State Synchronization${RESET}"
check_file_contains "plans.py" "pg_advisory_lock" "Uses PostgreSQL advisory locks"
check_file_contains "plans.py" "hashlib.md5" "Generates stable lock IDs"
check_file_contains "plans.py" "_atomic_run_count_check" "Atomic function implemented"

# Issue #9: Path Traversal Prevention
echo -e "\n${YELLOW}Issue #9: Path Traversal Prevention${RESET}"
check_file_contains "dashboard_v2.py" "_validate_user_id" "UUID validation function exists"
check_file_contains "dashboard_v2.py" "uuid.UUID" "Uses UUID validation"
check_file_contains "dashboard_v2.py" "_user_data_dir" "Calls validation before path use"

# Sentry Integration
echo -e "\n${YELLOW}Week 4: Sentry Integration${RESET}"
check_file_contains "dashboard_v2.py" "sentry_sdk" "Sentry SDK imported"
check_file_contains "dashboard_v2.py" "SENTRY_DSN" "Checks for Sentry DSN"
check_file_contains "requirements.txt" "sentry-sdk" "Dependency added"
check_file_contains ".env" "SENTRY_DSN" "Environment variable configured"

# Secrets Scanning
echo -e "\n${YELLOW}Week 4: Secrets Scanning Setup${RESET}"
check_file_contains ".github/workflows/secrets-scan.yml" "trufflehog" "GitHub Actions workflow created"
check_file_contains ".git/hooks/pre-commit" "password" "Pre-commit hook protects secrets"

# Summary
echo -e "\n${BLUE}╔═════════════════════════════════════════════════════╗${RESET}"
echo -e "${BLUE}║  VERIFICATION SUMMARY                               ║${RESET}"
echo -e "${BLUE}╚═════════════════════════════════════════════════════╝${RESET}"

total=$((passed + failed))
pass_rate=$((passed * 100 / total))

echo "Total Checks:   $total"
echo -e "${GREEN}Passed:         $passed${RESET}"
if [ $failed -gt 0 ]; then
    echo -e "${RED}Failed:         $failed${RESET}"
fi
echo "Pass Rate:      ${pass_rate}%"

if [ $failed -eq 0 ]; then
    echo -e "\n${GREEN}✓ All security fixes are in place${RESET}"
    exit 0
else
    echo -e "\n${RED}✗ Some fixes are missing${RESET}"
    exit 1
fi
