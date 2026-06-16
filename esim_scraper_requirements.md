# eSIM.dog Price Scraper Requirements

## Types of URLs & Scraping Logic

### Type 1: Simple (Basic)
- **Example**: `https://esim.dog/jp?tab=fixedgb&data=3&validity=14`
- **Logic**: 
  - Scroll to "Payment Summary" section
  - Extract "One-time payment" value
  - Straightforward extraction

### Type 2: VPN Checkbox (Tricky)
- **Example**: `https://esim.dog/th?tab=fixedgb&data=10&validity=14&vpn=true`
- **Issues**: 
  - URL might have `vpn=true` parameter (unwanted service)
  - Script must uncheck VPN option
  - Then recalculate and get new price
- **Logic**:
  1. Check if VPN is enabled (look for checkbox state)
  2. If enabled, uncheck it (simulate click)
  3. Wait for price recalculation
  4. Extract "One-time payment" value

### Type 3: Route Selection (Complex)
- **Example**: `https://esim.dog/jp?tab=fixedgb&data=1&validity=1`
- **Issues**:
  - Page has 5-6 different "Route" options
  - Each route = different network provider = different price
  - Site doesn't auto-select cheapest
  - Need to check ALL routes and select lowest price
- **Logic**:
  1. Identify all Route options
  2. For EACH route:
     - Click/select it
     - Extract its price
  3. Find the minimum price
  4. Extract that as final price
- **Visual Indicator**:
  - Green badge = cheaper than default
  - Yellow/Orange badge = more expensive
  - Must check each one

## Key Observations
- Payment Summary is consistent location
- "One-time payment" is the target field
- Need to handle dynamic content (VPN checkboxes, Route selection)
- Must use browser automation (Puppeteer/Playwright) not just HTML parsing
