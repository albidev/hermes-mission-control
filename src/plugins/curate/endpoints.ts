/**
 * Curate plugin endpoints — fetch API helpers.
 * 
 * This module is self-contained: it exports functions that the CurateRoute
 * component uses to interact with the backend. The plugin registry resolves
 * URLs from the manifest, so this module doesn't hardcode paths.
 */
import type {
  MissionControlCandidate,
  MissionControlVaultInfo,
} from '../../lib/hermes-api';

// Import existing functions from hermes-api (they still work as fallback)
// These will be replaced by registry-based resolution in a future step
import {
  loadMissionControlCandidates,
  loadMissionControlVaults,
  approveCandidate,
  rejectCandidate,
} from '../../lib/hermes-api';

// Re-export for use in CurateRoute
export {
  loadMissionControlCandidates,
  loadMissionControlVaults,
  approveCandidate,
  rejectCandidate,
};

// Type re-exports
export type {
  MissionControlCandidate,
  MissionControlVaultInfo,
};
