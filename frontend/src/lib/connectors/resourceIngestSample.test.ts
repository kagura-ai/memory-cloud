import { describe, expect, it } from "vitest";

import {
  RESOURCE_INGEST_SAMPLE_BODIES,
  connectorCurlSample,
  resourceTokenGuideCurlSample,
} from "./resourceIngestSample";

// Pull the single-quoted -d body out of a curl sample and parse it.
function dataBody(sample: string): unknown {
  const match = sample.match(/-d '([\s\S]*)'$/);
  expect(match, "sample has a single-quoted -d body").not.toBeNull();
  return JSON.parse(match![1]);
}

// Mirrors ResourceEventRequest's upsert rules (backend/src/models/schemas.py):
// op=upsert needs a doc_id of 1-255 chars, an integer version >= 1 and a
// non-empty payload. The backend test validates the same fixture against the
// real model; this one pins the rendered text to the fixture.
function expectAcceptedUpsert(body: unknown) {
  const b = body as Record<string, unknown>;
  expect(b.op).toBe("upsert");
  expect(typeof b.doc_id).toBe("string");
  expect((b.doc_id as string).length).toBeGreaterThanOrEqual(1);
  expect((b.doc_id as string).length).toBeLessThanOrEqual(255);
  expect(Number.isInteger(b.version)).toBe(true);
  expect(b.version as number).toBeGreaterThanOrEqual(1);
  expect(b.payload).toBeTypeOf("object");
  expect(Object.keys(b.payload as object).length).toBeGreaterThan(0);
}

describe("resource-ingest curl samples (#1756)", () => {
  it("connector dialog sample sends an upsert body the API accepts", () => {
    const sample = connectorCurlSample(
      "https://api.example.test",
      "res-123",
      "tok",
    );
    const body = dataBody(sample);
    expect(body).toEqual(RESOURCE_INGEST_SAMPLE_BODIES.connectorTest);
    expectAcceptedUpsert(body);
  });

  it("connector dialog sample single-quotes the URL and token", () => {
    const sample = connectorCurlSample(
      "https://api.example.test",
      "res-123",
      "a$b`c",
    );
    expect(sample).toContain(
      "curl -X POST 'https://api.example.test/api/v1/resources/res-123/events'",
    );
    expect(sample).toContain("-H 'X-Resource-API-Key: a$b`c'");
  });

  it("resource tokens tab sample sends an upsert body the API accepts", () => {
    const body = dataBody(resourceTokenGuideCurlSample());
    expect(body).toEqual(RESOURCE_INGEST_SAMPLE_BODIES.resourceTokenGuide);
    expectAcceptedUpsert(body);
  });

  it("every fixture body is an accepted upsert", () => {
    for (const body of Object.values(RESOURCE_INGEST_SAMPLE_BODIES)) {
      expectAcceptedUpsert(body);
    }
  });
});
