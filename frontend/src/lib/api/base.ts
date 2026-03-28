/**
 * API Client Utility
 *
 * Provides a centralized fetch wrapper for communicating with the Kagura Memory Cloud backend API.
 * Handles authentication, error handling, and response parsing.
 */

const API_BASE_URL = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8080';

export interface ApiError {
  error?: string;  // Error code from backend (e.g., "RES-001", "AUTH-001")
  message: string;
  status: number;
  details?: unknown;
}

export class ApiClient {
  private baseUrl: string;

  constructor(baseUrl: string = API_BASE_URL) {
    this.baseUrl = baseUrl;
  }

  /**
   * Generic fetch wrapper with error handling
   */
  private async fetch<T>(
    endpoint: string,
    options: RequestInit = {}
  ): Promise<T> {
    const url = `${this.baseUrl}${endpoint}`;

    const defaultHeaders: HeadersInit = {
      'Content-Type': 'application/json',
    };

    // Merge headers
    const headers = {
      ...defaultHeaders,
      ...(options.headers || {}),
    };

    try {
      const response = await fetch(url, {
        ...options,
        headers,
        credentials: 'include', // Include cookies for session management
      });

      // Handle non-OK responses
      if (!response.ok) {
        const errorBody = await response.text();
        let errorDetails;
        try {
          errorDetails = JSON.parse(errorBody);
        } catch {
          errorDetails = { message: errorBody };
        }

        // Backend returns { error: "RES-001", message: "...", details: {...} }
        // FastAPI may also return { detail: "..." }
        const errorMessage = errorDetails.message || errorDetails.detail || `HTTP ${response.status}: ${response.statusText}`;
        const errorCode = errorDetails.error;

        throw {
          error: errorCode,
          message: errorMessage,
          status: response.status,
          details: errorDetails.details || errorDetails,
        } as ApiError;
      }

      // Handle empty responses (e.g., 204 No Content)
      const contentLength = response.headers.get('content-length');
      const contentType = response.headers.get('content-type');

      // No content or empty body
      if (response.status === 204 || contentLength === '0' || !contentType) {
        return {} as T;
      }

      // Non-JSON response
      if (!contentType.includes('application/json')) {
        return {} as T;
      }

      return (await response.json()) as T;
    } catch (error) {
      // Re-throw ApiError
      if ((error as ApiError).status) {
        throw error;
      }

      // Wrap network errors
      throw {
        message: error instanceof Error ? error.message : 'Network error',
        status: 0,
        details: error,
      } as ApiError;
    }
  }

  /**
   * GET request
   */
  async get<T>(endpoint: string, options: RequestInit = {}): Promise<T> {
    return this.fetch<T>(endpoint, {
      ...options,
      method: 'GET',
    });
  }

  /**
   * POST request
   */
  async post<T>(
    endpoint: string,
    body?: unknown,
    options: RequestInit = {}
  ): Promise<T> {
    return this.fetch<T>(endpoint, {
      ...options,
      method: 'POST',
      body: body ? JSON.stringify(body) : undefined,
    });
  }

  /**
   * PUT request
   */
  async put<T>(
    endpoint: string,
    body?: unknown,
    options: RequestInit = {}
  ): Promise<T> {
    return this.fetch<T>(endpoint, {
      ...options,
      method: 'PUT',
      body: body ? JSON.stringify(body) : undefined,
    });
  }

  /**
   * PATCH request
   */
  async patch<T>(
    endpoint: string,
    body?: unknown,
    options: RequestInit = {}
  ): Promise<T> {
    return this.fetch<T>(endpoint, {
      ...options,
      method: 'PATCH',
      body: body ? JSON.stringify(body) : undefined,
    });
  }

  /**
   * DELETE request
   */
  async delete<T>(endpoint: string, options: RequestInit = {}): Promise<T> {
    return this.fetch<T>(endpoint, {
      ...options,
      method: 'DELETE',
    });
  }
}

// Singleton instance
export const apiClient = new ApiClient();

// ============================================================================
// User Profile API (Issue #221: i18n support)
// ============================================================================

export interface UserProfile {
  user_id: string;
  email: string;
  display_name: string | null;
  locale: string;
  created_at: string;
}

export interface UpdateUserProfileRequest {
  display_name?: string;
  locale?: string;
}

/**
 * Get current user profile
 */
export async function getUserProfile(): Promise<UserProfile> {
  return apiClient.get<UserProfile>('/api/v1/users/profile');
}

/**
 * Update current user profile
 */
export async function updateUserProfile(
  data: UpdateUserProfileRequest
): Promise<UserProfile> {
  return apiClient.put<UserProfile>('/api/v1/users/profile', data);
}
