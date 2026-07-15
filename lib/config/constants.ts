// 'llm-gateway-*' 이름은 기존(원본 블루프린트) 스택이 점유 중이라 충돌 방지를 위해 별도 프리픽스 사용
// The 'llm-gateway-*' names are taken by the existing (original blueprint) stack, so a separate prefix is used to avoid collisions
export const PROJECT_NAME = 'llm-gw-gs';

// 최신 LiteLLM 안정화(stable) 이미지 태그.
// https://github.com/BerriAI/litellm/pkgs/container/litellm 에서 main-vX.Y.Z-stable 태그 확인 후 갱신.
// 배포 시 -c litellmImageTag=... 로 오버라이드 가능.
// Latest LiteLLM stable image tag.
// Check the main-vX.Y.Z-stable tags at https://github.com/BerriAI/litellm/pkgs/container/litellm and update.
// Can be overridden at deploy time with -c litellmImageTag=...
export const LITELLM_IMAGE_TAG = 'main-v1.83.14-stable';

export const DEFAULT_REGION = 'ap-northeast-2';

export type ModelPrefix = 'us' | 'eu' | 'apac' | 'global';

/**
 * Bedrock cross-region inference profile 프리픽스를 결정한다.
 * -c modelPrefix=us|eu|apac|global 로 오버라이드 가능.
 *
 * 기본값 global: Claude 4.6/4.5 세대는 지역(apac/eu) 프로필 없이
 * global 프로필로만 제공되는 리전이 많다 (ap-northeast-2 확인됨).
 *
 * Determines the Bedrock cross-region inference profile prefix.
 * Can be overridden with -c modelPrefix=us|eu|apac|global.
 *
 * Default global: for the Claude 4.6/4.5 generation, many regions offer only the
 * global profile without regional (apac/eu) profiles (confirmed for ap-northeast-2).
 */
export function resolveModelPrefix(_region: string, override?: string): ModelPrefix {
  if (override === 'us' || override === 'eu' || override === 'apac' || override === 'global') {
    return override;
  }
  return 'global';
}

export interface ModelIds {
  opus: string;
  sonnet: string;
  haiku: string;
}

export function modelsFor(prefix: ModelPrefix): ModelIds {
  return {
    // 사용자 기본(default) 모델 — Opus 4.8 / User default model — Opus 4.8
    opus: `${prefix}.anthropic.claude-opus-4-8`,
    sonnet: `${prefix}.anthropic.claude-sonnet-4-6`,
    haiku: `${prefix}.anthropic.claude-haiku-4-5-20251001-v1:0`,
  };
}

export const BUDGET = {
  // Virtual Key 발급 시 적용되는 사용자별 기본 예산 / Default per-user budget applied when issuing a Virtual Key
  MONTHLY_LIMIT_USD: 1000,
  BUDGET_DURATION: '30d',
} as const;

// NestedStack 간 순환 참조를 피하기 위한 고정 테이블 이름 / Fixed table names to avoid circular references between NestedStacks
export const AUDIT_TABLE_NAME = `${PROJECT_NAME}-audit`;
export const CONFIG_TABLE_NAME = `${PROJECT_NAME}-config`;
