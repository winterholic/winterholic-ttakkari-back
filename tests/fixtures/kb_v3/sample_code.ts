export interface IngestItem {
    id: string;
    filename: string;
}

export class PipelineManager {
    runStage(stage: string): boolean {
        return true;
    }
}
