# Ticket Engine

## Status

Active — strongest backend/systems engineering project.

## Summary

High-concurrency ticket booking system designed to handle large numbers of simultaneous booking requests without duplicate bookings or overselling.

## Problem

Traditional ticket booking systems fail under high concurrent load, leading to race conditions, duplicate bookings, and overselling.

## Solution

Distributed ticket allocation using Redis for atomic operations, in-memory state, and queue-based worker processing with MySQL persistence.

## Architecture

```text
User
 ↓
Go API
 ↓
Redis
 ↓
Worker
 ↓
MySQL
```

Redis handles:
- In-memory ticket state
- Atomic ticket allocation (LPOP)
- Queueing and Pub/Sub
- Real-time ticket count
- Concurrency control

Event processing uses Redis queues/Pub/Sub and Watermill where applicable.

## Technologies

Go, MySQL, Redis, React, TypeScript, Docker, Docker Compose, k6

## My Contribution

Full-stack development including backend API, worker pipeline, Redis integration, frontend dashboard, and load testing.

## Features

- User login
- Queue simulation
- Real-time dashboard (polling-based; WebSockets planned)
- Ticket/seat visualization
- Atomic ticket allocation
- Load-tested concurrency handling

## Verified Metrics

**Status: PROJECT-REPORTED — use in resume/applications only when supported by project documentation/test results.**

| Metric | Value | Verification |
|--------|-------|--------------|
| Concurrent users (simulated) | 10,000 | PROJECT-REPORTED |
| Throughput | ~3,000 requests/sec | PROJECT-REPORTED |
| Average latency | ~120 ms | PROJECT-REPORTED |
| P95 latency | < 250 ms | PROJECT-REPORTED |
| Ticket allocations/sec | ~2,500 | PROJECT-REPORTED |
| Duplicate bookings | 0 | PROJECT-REPORTED |
| Overselling | None during test | PROJECT-REPORTED |

## Technical Concepts

- Concurrency and atomic operations
- Distributed coordination
- Caching and queues
- Event processing
- High-throughput APIs
- Database persistence
- Load testing with k6
- Docker containerization
- Race-condition prevention
- System design and performance optimization

## Relevant Roles

- Backend Engineer
- Software Engineer / SDE
- Product Engineer
- Distributed Systems Engineer
- Platform/Infrastructure Engineer

## Resume-Worthy Facts

- Designed and built high-concurrency ticket booking system
- Implemented atomic ticket allocation using Redis
- Built Go API with Redis worker pipeline and MySQL persistence
- Developed React/TypeScript frontend with real-time dashboard
- Conducted load testing demonstrating zero duplicate bookings

## Verified Claims

- Architecture and technology stack
- Engineering concepts demonstrated
- Feature set
- Load testing was performed

## Unverified Claims

- Exact benchmark numbers (project-reported, pending documentation verification)
- WebSockets (planned, not yet implemented)

## Source References

- RIBHU_CAREER_CONTEXT.md — Section 11
