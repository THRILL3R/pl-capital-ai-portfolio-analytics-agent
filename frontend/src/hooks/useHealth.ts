import { useEffect, useState } from 'react';
import { api } from '../api/client';
import type { Health } from '../api/types';

export type HealthState = { state: 'loading' } | { state: 'offline' } | { state: 'ready'; health: Health };

/** One-shot status check for the header chip. Never blocks rendering. */
export function useHealth(): HealthState {
  const [value, setValue] = useState<HealthState>({ state: 'loading' });
  useEffect(() => {
    let active = true;
    api
      .health()
      .then((health) => active && setValue({ state: 'ready', health }))
      .catch(() => active && setValue({ state: 'offline' }));
    return () => {
      active = false;
    };
  }, []);
  return value;
}
