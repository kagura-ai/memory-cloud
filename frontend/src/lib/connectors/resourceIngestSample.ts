// #1756: the copy-paste curl samples for the resource-ingest API
// (POST /api/v1/resources/{id}/events). Both UI samples — the connector-created
// dialog and the Resource tokens tab guide — take their -d body from
// resourceIngestSampleBodies.json, which backend/tests/models/
// test_resource_ingest_samples.py validates against ResourceEventRequest, so a
// sample the API would refuse fails CI instead of the user's terminal.
import bodies from "./resourceIngestSampleBodies.json";

export const RESOURCE_INGEST_SAMPLE_BODIES = bodies;

// #893: copy-pastable curl for the connector-created dialog (manual CLI test:
// verify events become memories without a worker). Single-quote the URL and
// header value so a token with shell metacharacters is safe to paste; the JSON
// body has no single quotes, so the single-quoted -d stays valid.
export function connectorCurlSample(
  apiBaseUrl: string,
  resourceId: string,
  token: string,
): string {
  return [
    `curl -X POST '${apiBaseUrl}/api/v1/resources/${resourceId}/events' \\`,
    `  -H 'X-Resource-API-Key: ${token}' \\`,
    `  -H 'Content-Type: application/json' \\`,
    `  -d '${JSON.stringify(bodies.connectorTest)}'`,
  ].join("\n");
}

// The Resource tokens tab guide sample: placeholders instead of real values,
// pretty-printed body.
export function resourceTokenGuideCurlSample(): string {
  return [
    `curl -X POST "http://localhost:8080/api/v1/resources/{resource_id}/events" \\`,
    `  -H "X-Resource-API-Key: YOUR_TOKEN_HERE" \\`,
    `  -H "Content-Type: application/json" \\`,
    `  -d '${JSON.stringify(bodies.resourceTokenGuide, null, 2)}'`,
  ].join("\n");
}
