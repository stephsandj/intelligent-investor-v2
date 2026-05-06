#!/bin/bash
echo "Checking CSRF implementation..."
grep -n "_csrf_origin_check\|Forbidden.*invalid_origin" dashboard_v2.py | head -10
echo ""
echo "CSRF checks found:"
grep -c "_csrf_origin_check()" dashboard_v2.py
echo "instances of CSRF validation calls"
