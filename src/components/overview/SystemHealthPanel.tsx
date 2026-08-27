import { useI18n } from '../../lib/i18n';
import { Cpu, Disc, Fan, HardDrive, Server, Thermometer } from 'lucide-react';
import type { MissionControlMachineStatus } from '../../lib/hermes-api';
import { Card } from '../ui/Card';
import { Badge } from '../ui/Badge';

type SystemHealthPanelProps = {
  machine: MissionControlMachineStatus;
  gatewayStatus: string;
  backendHealth: 'healthy' | 'degraded' | 'offline';
};

function MiniBar({
  value,
  max = 100,
  variant = 'default',
}: {
  value: number | null;
  max?: number;
  variant?: 'default' | 'positive' | 'warning' | 'negative';
}) {
  const pct = value !== null ? Math.min(100, Math.max(0, (value / max) * 100)) : 0;
  const colorClass =
    variant === 'negative' ? 'bg-negative' :
    variant === 'warning' ? 'bg-warning' :
    variant === 'positive' ? 'bg-positive' :
    'bg-accent';

  return (
    <div className="h-1.5 w-full rounded-full bg-surface-sunken overflow-hidden">
      <div
        className={`h-full rounded-full ${colorClass} transition-all duration-500`}
        style={{ width: `${pct}%` }}
      />
    </div>
  );
}

/**
 * Discrete 5-notch level meter for thermal pressure. Thermal is a coarse
 * text level (Nominal/Low/Moderate/Heavy/Extreme), so a continuous bar would
 * imply false precision. Each filled notch = one level step.
 */
const LEVEL_ORDER = ['nominal', 'low', 'moderate', 'heavy', 'extreme'];

function LevelMeter({ level }: { level: string | null }) {
  const active = level ? LEVEL_ORDER.indexOf(level.toLowerCase()) + 1 : 0;
  const colorClass =
    active >= 5 ? 'bg-negative' :
    active >= 3 ? 'bg-warning' :
    'bg-positive';

  return (
    <div className="flex w-full gap-1">
      {Array.from({ length: 5 }, (_, i) => (
        <div
          key={i}
          className={`h-1.5 flex-1 rounded-full ${i < active ? colorClass : 'bg-surface-sunken'} transition-colors duration-500`}
        />
      ))}
    </div>
  );
}

function HealthMetric({
  icon: Icon,
  label,
  value,
  unit,
  bar,
  barVariant = 'default',
  level,
  caption,
}: {
  icon: React.ElementType;
  label: string;
  value: string | number | null;
  unit?: string;
  bar?: number | null;
  barVariant?: 'default' | 'positive' | 'warning' | 'negative';
  level?: string | null;
  caption?: string | null;
}) {
  return (
    <div className="flex flex-col gap-1">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-1.5">
          <Icon className="h-3.5 w-3.5 text-text-muted" />
          <span className="text-xs text-text-muted">{label}</span>
        </div>
        <span className="text-xs font-mono text-text">
          {value ?? '—'}{unit ?? ''}
        </span>
      </div>
      {level !== undefined ? (
        <LevelMeter level={level} />
      ) : bar !== undefined ? (
        <MiniBar value={bar} variant={barVariant} />
      ) : null}
      {caption ? (
        <span className="text-[10px] text-text-muted">{caption}</span>
      ) : null}
    </div>
  );
}

export function SystemHealthPanel({
  machine,
  gatewayStatus,
  backendHealth,
}: SystemHealthPanelProps) {
  const { t } = useI18n();
  const healthVariant =
    machine.health === 'healthy' ? 'positive' :
    machine.health === 'degraded' ? 'warning' :
    machine.health === 'critical' ? 'negative' : 'default';

  const loadDerivedCpuPercent =
    machine.loadAverage?.perCore !== null && machine.loadAverage?.perCore !== undefined
      ? machine.loadAverage.perCore * 100
      : null;

  const cpuUsagePercent =
    machine.cpuUsagePercent !== null && machine.cpuUsagePercent !== undefined && machine.cpuUsagePercent > 0.5
      ? machine.cpuUsagePercent
      : loadDerivedCpuPercent;

  const cpuLoadVariant =
    (cpuUsagePercent ?? 0) > 85 ? 'negative' :
    (cpuUsagePercent ?? 0) > 65 ? 'warning' :
    'positive';

  const ramUsagePercent = machine.ramUsage?.usedPercent ?? null;
  const ramVariant =
    (ramUsagePercent ?? 0) > 90 ? 'negative' :
    (ramUsagePercent ?? 0) > 75 ? 'warning' :
    'positive';

  const diskUsedGb =
    machine.diskUsage.totalGb > 0
      ? Math.max(0, Number((machine.diskUsage.totalGb - machine.diskUsage.freeGb).toFixed(1)))
      : null;

  return (
    <Card padding="none">
      <div className="flex items-center justify-between px-4 pt-4 pb-3 border-b border-border-subtle">
        <div className="flex flex-col gap-0.5">
          <span className="eyebrow">{t('health.system')}</span>
          <h2 className="text-sm font-semibold text-text">{t('health.title')}</h2>
        </div>
        <Badge
          variant={
            backendHealth === 'healthy' ? 'positive' :
            backendHealth === 'degraded' ? 'warning' : 'negative'
          }
          dot
        >
          {gatewayStatus}
        </Badge>
      </div>

      <div className="p-3 flex flex-col gap-2.5">
        {/* Machine health */}
        <div className="flex items-center gap-2 pb-2 border-b border-border-subtle">
          <Server className="h-4 w-4 text-text-muted" />
          <span className="text-xs text-text">{machine.host}</span>
          <div className="flex-1" />
          <Badge variant={healthVariant} dot>
            {machine.health}
          </Badge>
        </div>

        {/* CPU Load */}
        <HealthMetric
          icon={Cpu}
          label="CPU load"
          value={cpuUsagePercent !== null ? `${cpuUsagePercent.toFixed(1)}%` : '—'}
          bar={cpuUsagePercent}
          barVariant={cpuLoadVariant}
        />

        {/* Memory */}
        <HealthMetric
          icon={Disc}
          label="RAM"
          value={
            machine.ramUsage?.usedGb !== null && machine.ramUsage?.totalGb !== null
              ? `${machine.ramUsage.usedGb} / ${machine.ramUsage.totalGb} GB`
              : machine.processMemoryMb !== null
              ? `${machine.processMemoryMb} MB`
              : '—'
          }
          bar={ramUsagePercent}
          barVariant={ramVariant}
        />

        {/* Disk */}
        <HealthMetric
          icon={HardDrive}
          label="Disk"
          value={
            diskUsedGb !== null && machine.diskUsage.totalGb > 0
              ? `${diskUsedGb} / ${machine.diskUsage.totalGb} GB`
              : '—'
          }
          bar={machine.diskUsage.usedPercent}
          barVariant={
            (machine.diskUsage.usedPercent ?? 0) > 90 ? 'negative' :
            (machine.diskUsage.usedPercent ?? 0) > 75 ? 'warning' :
            'positive'
          }
        />

        {/* Fan — only rendered when powermetrics exposes fan RPM (Intel/older macOS) */}
        {machine.thermal.fanRpm !== null && (
          <HealthMetric
            icon={Fan}
            label={machine.thermal.fanCount ? `Fan ×${machine.thermal.fanCount}` : 'Fan'}
            value={`${machine.thermal.fanRpm.toFixed(0)} rpm`}
          />
        )}

        {/* Thermal — unavailable (no sensor) is distinct from a nominal value */}
        {machine.thermal.source === 'unavailable' ? (
          <HealthMetric
            icon={Thermometer}
            label="Thermal"
            value="Unavailable"
            caption={machine.thermal.error ?? 'No thermal sensor detected on this host'}
          />
        ) : machine.thermal.thermalPressure !== null ? (
          <HealthMetric
            icon={Thermometer}
            label="Thermal"
            value={`${machine.thermal.thermalPressure.toFixed(1)}°C`}
            bar={machine.thermal.thermalPressure}
            barVariant={
              machine.thermal.thermalPressure > 85 ? 'negative' :
              machine.thermal.thermalPressure > 75 ? 'warning' :
              'positive'
            }
            caption={
              machine.thermal.source === 'sysfs-thermal' ? 'sysfs thermal zone' :
              machine.thermal.source === 'lm-sensors' ? 'lm-sensors' :
              machine.thermal.source ?? undefined
            }
          />
        ) : (
          <HealthMetric
            icon={Thermometer}
            label="Thermal"
            value={machine.thermal.thermalLevel ?? '—'}
            level={machine.thermal.thermalLevel ?? null}
          />
        )}
      </div>
    </Card>
  );
}
