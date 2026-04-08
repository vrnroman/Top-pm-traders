# Polymarket Copy-Trading Bot — Documentation Index

Welcome to the complete documentation for the Polymarket copy-trading bot. This guide helps you navigate all documentation resources.

---

## Quick Navigation

### For New Users
Start here to get the bot running:
1. **[Setup Guide](./setup-guide.md)** — Installation, configuration, .env setup, Docker deployment
2. **[README.md in root](../README.md)** — Quick start, features overview

### For Developers
Understand the codebase and architecture:
1. **[Codebase Summary](./codebase-summary.md)** — File structure, modules, key functions (start here!)
2. **[System Architecture](./system-architecture.md)** — Data flow, component interactions, design principles
3. **[Code Standards](./code-standards.md)** — Patterns, conventions, testing, blockchain integration (viem)
4. **[Project Overview & PDR](./project-overview-pdr.md)** — Specification, design decisions, acceptance criteria

### For Project Management
Track status and plan improvements:
1. **[Backlog](./backlog.md)** — Feature status, recent completions, known issues, roadmap (Tier 1–4)
2. **[Project Changelog](./project-changelog.md)** — Version history, viem migration details, CVE fixes

---

## Documentation Structure

```
docs/
├── README.md                    # You are here (navigation hub)
├── setup-guide.md              # Configuration & deployment instructions (Russian)
├── codebase-summary.md         # File structure, modules, quick reference
├── system-architecture.md      # Data flow, component design, technology stack
├── code-standards.md           # Patterns, conventions, viem patterns, testing
├── project-overview-pdr.md     # Full specification, design decisions, acceptance criteria
├── project-changelog.md        # Version history, recent changes, migration notes
└── backlog.md                  # Feature status, roadmap, known issues
```

---

## Document Descriptions

### 📋 [Setup Guide](./setup-guide.md) — Configuration & Deployment
**For:** First-time users, DevOps

**Contains:**
- MetaMask wallet setup on Polygon
- `.env` configuration (wallets, risk limits, API keys)
- Docker deployment
- RPC provider configuration
- Emergency actions (sell-all, auto-redeem)
- Security best practices

**Language:** Russian (Русский)

**When to use:** Before running the bot

---

### 📚 [Codebase Summary](./codebase-summary.md) — Project Overview
**For:** Developers new to the project

**Contains:**
- File-by-file module breakdown
- Entry point explanation (index.ts)
- Configuration & validation layer
- Trade monitoring & execution flow
- Inventory & storage
- Blockchain integration (viem)
- Testing overview
- Build & deployment commands
- Quick start guide

**When to use:** Getting oriented with the codebase

---

### 🏗️ [System Architecture](./system-architecture.md) — Design & Data Flow
**For:** Developers implementing features, code reviewers

**Contains:**
- System overview diagram
- Component responsibilities (9 modules)
- Data flow examples (buy/sell/redeem)
- Periodic tasks (5-min sync, 30-min redeem)
- Technology stack
- Design principles

**When to use:** Understanding how modules interact, implementing new features

---

### ⚙️ [Code Standards](./code-standards.md) — Patterns & Conventions
**For:** Developers writing code

**Contains:**
- Project organization (file structure)
- TypeScript configuration
- Viem patterns (wallet client, public client, readContract, writeContract)
- Config & env vars
- Shared types
- Risk manager pattern
- Async patterns (sleep, retry, singleton caching)
- Error handling
- Testing patterns (Vitest)
- Naming conventions
- Linting & formatting
- Performance considerations
- Security best practices
- Module dependencies (DAG)
- Ethers → Viem migration guide

**When to use:** Before writing code, code review

---

### 📖 [Project Overview & PDR](./project-overview-pdr.md) — Specification
**For:** Project managers, stakeholders, architects

**Contains:**
- Project vision & core features (8 features, all COMPLETE)
- Technical architecture overview
- Functional requirements (10 MUSTs)
- Non-functional requirements (8 SHOULDs)
- Design decisions (viem migration, inventory sync, auto-redeem)
- Acceptance criteria (definition of done)
- Known limitations
- Future roadmap (Tier 2–4)
- Success metrics
- Dependencies & constraints
- Maintenance tasks
- Version history
- Stakeholders & ownership

**When to use:** Planning features, evaluating design decisions, scope management

---

### 📝 [Project Changelog](./project-changelog.md) — Version History
**For:** Tracking changes, understanding evolution

**Contains:**
- v1.0.0 (2026-04-03): Viem migration, 0 CVEs, new tests
- v0.9.0 (2026-04-02): Auto-redeem, type coverage, 45+ tests
- v0.8.0 (2026-04-01): Hostile audit fixes
- v0.1.0 (2026-03-30): Initial deployment
- Known issues
- Technical debt

**When to use:** Understanding what changed in recent releases

---

### ✅ [Backlog](./backlog.md) — Status & Roadmap
**For:** Feature planning, priority management

**Contains:**
- Recent completions (viem migration, hostile audit fixes, auto-redeem, portfolio refactor)
- Module inventory (31 files + 6 test files)
- Tier 1 (production blockers) — mostly done
- Tier 2 (important improvements) — health endpoint, dashboard, trader re-screening
- Tier 3 (competitive edge) — WebSocket, on-chain monitoring, multi-wallet
- Tier 4 (advanced/v2) — news signals, multi-strategy, AI
- Known technical debt

**When to use:** Prioritizing next features, understanding scope

---

## How to Use This Documentation

### Scenario 1: "I'm deploying the bot for the first time"
1. Read **README.md** (root) for overview
2. Follow **Setup Guide** for configuration
3. Check **Codebase Summary** to understand what's running

### Scenario 2: "I need to add a new feature"
1. Read **Project Overview & PDR** for requirements
2. Check **System Architecture** for where it fits
3. Review **Code Standards** for patterns
4. Use **Codebase Summary** as module reference
5. Add tests, update docs after implementation

### Scenario 3: "There's a bug; I need to understand the code flow"
1. Check **System Architecture** for data flow
2. Read **Codebase Summary** for file locations
3. Use **Code Standards** for pattern reference
4. Debug with logs in `logs/bot-YYYY-MM-DD.log`

### Scenario 4: "I'm reviewing a pull request"
1. Check **Code Standards** for pattern compliance
2. Cross-reference **System Architecture** for design impact
3. Review **Project Overview & PDR** for acceptance criteria

### Scenario 5: "I need to understand a design decision"
1. Read **Project Overview & PDR** → "Design Decisions" section
2. Check **Project Changelog** for migration notes
3. Review **Backlog** for known issues related to design

---

## Key Statistics

| Metric | Value |
|--------|-------|
| **Production files** | 31 (TypeScript) |
| **Test files** | 9 (Vitest) |
| **Total lines of code** | ~2,500 |
| **Test lines** | ~1,000 |
| **Unit test coverage** | 45+ tests |
| **Vulnerabilities** | 0 CVEs |
| **TypeScript strict** | ✓ No `any` types |
| **ESLint** | ✓ Zero violations |
| **Documentation** | 2,300 lines across 7 docs |

---

## Important Links

### In This Repository
- **[README.md](../README.md)** — Project overview, quick start
- **[package.json](../package.json)** — Dependencies, scripts
- **[.env.example](../.env.example)** — Configuration template
- **[docker-compose.yml](../docker-compose.yml)** — Docker deployment config
- **[tsconfig.json](../tsconfig.json)** — TypeScript settings
- **[src/](../src/)** — Source code directory

### External Resources
- [Polymarket](https://polymarket.com) — Prediction markets platform
- [Viem Documentation](https://viem.sh) — Blockchain client library
- [@polymarket/clob-client](https://github.com/polymarket/clob-client) — CLOB API wrapper
- [Vitest](https://vitest.dev) — Unit testing framework
- [Polygon RPC](https://polygon-rpc.com) — Polygon network endpoint

---

## Documentation Maintenance

### Update Triggers
- **Code changes** → Update Code Standards, Codebase Summary
- **Feature implementation** → Update Backlog, Project Changelog
- **Design changes** → Update System Architecture, Project Overview & PDR
- **Deployment changes** → Update Setup Guide

### Review Checklist
- [ ] All file paths are correct
- [ ] Code examples compile/run
- [ ] Diagrams are up-to-date
- [ ] Links are valid
- [ ] No contradictions between docs
- [ ] Latest changes documented

### Document Owners
| Document | Owner | Last Updated |
|----------|-------|--------------|
| Setup Guide | Pavel Volkov | 2026-04-02 |
| Codebase Summary | Pavel Volkov | 2026-04-03 |
| System Architecture | Pavel Volkov | 2026-04-03 |
| Code Standards | Pavel Volkov | 2026-04-03 |
| Project Overview & PDR | Pavel Volkov | 2026-04-03 |
| Project Changelog | Pavel Volkov | 2026-04-03 |
| Backlog | Pavel Volkov | 2026-04-03 |

---

## FAQ

**Q: Where do I configure the bot?**
A: See `Setup Guide` → `.env` configuration section. Use `.env.example` as template.

**Q: How do I deploy to production?**
A: See `Setup Guide` → Docker section. Or see `Codebase Summary` → Build & Deployment.

**Q: What's the viem migration?**
A: See `Project Changelog` → v1.0.0 section. Also `Code Standards` → Ethers → Viem migration guide.

**Q: How does risk management work?**
A: See `System Architecture` → Risk Manager component. Also `Code Standards` → Risk Manager Pattern.

**Q: What are the known issues?**
A: See `Backlog` → Known Technical Debt. Also `Project Overview & PDR` → Known Limitations.

**Q: How do I run tests?**
A: `npm test` or `npm run test:watch`. See `Codebase Summary` → Testing section.

**Q: How do I add a new feature?**
A: See top of this README → Scenario 2. Also `Project Overview & PDR` → Future Roadmap.

---

## Report & Contact

**Documentation Last Updated:** 2026-04-03

**Status:** Complete and verified against codebase (v1.0.0 with viem migration)

**Questions?** Contact Pavel Volkov (project developer) or check logs in `logs/bot-YYYY-MM-DD.log` for runtime issues.

---

## Quick Reference Commands

```bash
# Development
npm run dev                    # Run with auto-reload
npm test                      # Run tests
npm run lint:fix              # Fix linting issues
npm run format                # Format code

# Deployment
npm run build                 # Compile
npm start                      # Run production
docker compose up -d          # Docker deployment

# Utilities
npm run health-check          # Test API connectivity
npx tsx src/scripts/screen-traders.ts       # Trader analysis
npx tsx src/scripts/sell-all.ts             # Liquidate positions
npx tsx src/scripts/performance-report.ts   # P&L analysis
```

---

**Happy trading! 🚀**
