# Design Rules

> Horcrux Vision UI Critic이 스크린샷 평가 시 참조하는 디자인 기준.
> 프로젝트별로 커스터마이징 가능.

## Color Palette

- primary: #2563EB (blue-600)
- secondary: #7C3AED (violet-600)
- success: #16A34A (green-600)
- warning: #F59E0B (amber-500)
- error: #DC2626 (red-600)
- background: #FFFFFF (light) / #1E1E2E (dark)
- text: #1F2937 (gray-800) / #F9FAFB (gray-50, dark)
- contrast_ratio_min: 4.5

## Spacing

- grid: 4px
- padding_min: 8px
- section_gap: 16px ~ 32px
- consistent_spacing: true

## Typography

- heading_hierarchy: h1 > h2 > h3 (size 차이 명확)
- body_size_min: 14px
- line_height_min: 1.4
- max_line_length: 80ch
- font_consistency: true

## Layout & Alignment

- alignment: consistent (left-aligned or center, 혼합 금지)
- max_content_width: 1200px
- responsive_breakpoints: [375, 768, 1440]
- visual_balance: symmetric or intentional asymmetry

## Components

- button_min_height: 36px
- button_min_width: 80px
- input_min_height: 36px
- border_radius: consistent (같은 컴포넌트 그룹 내)
- hover_state: required for interactive elements

## References

<!-- 레퍼런스 이미지 경로 (선택사항) -->
<!-- - ref_image: .horcrux/refs/ideal-dashboard.png -->
<!-- - ref_image: .horcrux/refs/ideal-mobile.png -->
