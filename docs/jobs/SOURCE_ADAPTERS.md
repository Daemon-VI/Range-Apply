# Source Adapters

**Phase 2 Source Ingestion Layer**

---

## 1. Adapter Framework

All source adapters implement the `JobSource` interface:

```python
class JobSource(ABC):
    @property
    @abstractmethod
    def source_type(self) -> JobSourceType: ...

    @abstractmethod
    async def discover(self, identifier: str, **kwargs) -> List[RawJob]: ...
```

This ensures the core ingestion pipeline has **no source-specific branching**.

---

## 2. Implemented Structured Adapters

### Greenhouse (`GreenhouseSource`)
- **Protocol**: Unauthenticated public JSON API
- **Endpoint**: `https://boards-api.greenhouse.io/v1/boards/{board_token}/jobs?content=true`
- **Cost**: ₹0, free tier, zero Firecrawl quota
- **Fields Extracted**: `id`, `title`, `location.name`, `content` (HTML), `departments`, `offices`, `updated_at`, `requisition_id`

### Lever (`LeverSource`)
- **Protocol**: Unauthenticated public JSON API
- **Endpoint**: `https://api.lever.co/v0/postings/{company_slug}?mode=json`
- **Cost**: ₹0, free tier, zero Firecrawl quota
- **Fields Extracted**: `id`, `text`, `descriptionHtml`, `categories.location`, `categories.commitment`, `workplaceType`, `urls.apply`, `salaryRange`

### Ashby (`AshbySource`)
- **Protocol**: Unauthenticated public JSON API
- **Endpoint**: `https://api.ashbyhq.com/posting-api/job-board/{board_name}?includeCompensation=true`
- **Cost**: ₹0, free tier, zero Firecrawl quota
- **Fields Extracted**: `id`, `title`, `descriptionHtml`, `location`, `secondaryLocations`, `workplaceType`, `employmentType`, `compensationTierSummary`, `jobUrl`, `applyUrl`

---

## 3. Unstructured Web Fetching (`FirecrawlFetcher`)
Reserved for company career pages that lack public structured ATS feeds:
- Uses `Firecrawl.scrape()` for clean Markdown extraction
- Bounded retries with exponential backoff
- Rate limiting and keyless/keyed mode support
