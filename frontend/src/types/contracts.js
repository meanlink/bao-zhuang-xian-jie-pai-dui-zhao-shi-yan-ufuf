/** @typedef {{id:number,name:string,source:string,rows:number,checksum:string,released_scope:?string}} Dataset */
/** @typedef {{id:number,name:string,dataset_id:number,status:string,result:Object,approved:boolean}} AnalysisJob */
/** @typedef {{id:number,name:string,job_id:number,destination:string,next_run:number,enabled:boolean,scope:string,last_status:string,last_reason:string}} ReportSchedule */
/** @typedef {{id:number,report_id:number,created_at:number,checksum:string,filename:string,destination:string,status:string,scope:string}} Delivery */
export {};
