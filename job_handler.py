import json
from enum import Enum, IntEnum
import time
### HashCat Command Format: hashcat -a <attack_mode> -m <hash_type> <hash> <wordlist/mask/length> -w 4

class STATUS(IntEnum):
    CREATED = 1
    RUNNING = 2
    COMPLETED = 3
    FAILED = 4
    CANCELLED = 5
    QUEUED = 6
    PENDING = 7
    EXHAUSTED = 8

class Job:

    def __init__(self, job_id, _hash, hash_type, status, attack_mode, required_info, result_file=None):
        self.job_id = job_id
        self.hash = _hash
        self.hash_type = hash_type
        self.job_status = status
        self.attack_mode = attack_mode
        self.required_info = required_info
        self.progress = [0,0]
        self.result_file = result_file

    def __str__(self):
        return f"Job ID: {self.job_id} | Hash: {self.hash} | Hash Type: {self.hash_type} | Job Status: {self.job_status}"
    
    def to_json(self):
        return json.dumps(self, default=lambda o: o.__dict__, sort_keys=True, indent=4)
    
class JobHandler:

    def __init__(self, aws_controller, mode):
        self.aws_controller = aws_controller
        if mode == "client":
            self.outbound_queue = self.aws_controller.create_queue('deliveryQueue')
            self.control_queue = self.aws_controller.create_queue('controlQueue')
            self.inbound_queue = self.aws_controller.create_queue('returnQueue')
        else:
            for retry in range(5):
                self.outbound_queue = self.aws_controller.locate_queue('returnQueue')
                if self.outbound_queue is not None:
                    print("Located outbound queue")
                    break
                time.sleep(5)
            if self.outbound_queue is None:
                print("Error: Failed to locate outbound queue. Exiting...")
                exit(1)
        
        self.wordlist_bucket_name = None
        self.job_log = {}
        self.job_id = 1 
        

    def send_job(self, job):
        job.job_status = STATUS.QUEUED
        if job.required_info is not None and job.attack_mode == 0:
            if self.wordlist_bucket_name is None:
                self.wordlist_bucket_name = self.aws_controller.create_bucket("wordlist-bucket")
            file_name = self.get_file_name(job.required_info)
            response = self.aws_controller.upload_file(job.required_info, self.wordlist_bucket_name, file_name)
            if response == False:
                print(f"Error: Failed to create bucket for job {job.job_id}. Continuing...")
                job.job_status = STATUS.FAILED
                return
            
            job.required_info = (file_name, self.wordlist_bucket_name)
        if self.aws_controller.get_num_instances() < self.aws_controller.get_max_instances():
            self.aws_controller.create_instance()
        else:
            print("Max number of instances reached. Job queued.")
        response = self.aws_controller.message_queue(self.outbound_queue, job.to_json(), "Job")
        if response == False:
            print(f"Error: Failed to send job {job.job_id} to queue. Continuing...")
            job.job_status = STATUS.FAILED
            return
        else:
            print(f"Job {job.job_id} sent to queue.")
            self.job_log[job.job_id] = job ## TODO: need to move a way from using receipt handle. Consider moving away from FIFO queues
 
    
    def get_file_name(self, file_location):
        return file_location.split("/")[-1].split(".")[0]

    def get_new_job_id(self):
        num = self.job_id
        self.job_id += 1
        return num
    
    def create_job(self, _hash, hash_type, attack_mode, required_info):
        job = Job(self.get_new_job_id(), _hash, hash_type, STATUS.CREATED, attack_mode, required_info)
        return job
    
    def get_job(self, job_id):
        return self.job_log[job_id]
    
    def cancel_job(self, job_id):
        if job_id not in self.job_log:
            print(f"Error: Job {job_id} does not exist.")
            return False

        response = self.aws_controller.message_queue(self.control_queue, Command(job_id, REQUEST.CANCEL).to_json(), "Command")
        if response == False:
            print(f"Error: Failed to cancel job #{job_id}. The job may still be running.")
            return False
        else:
            self.job_log[job_id].job_status = STATUS.CANCELLED
            print(f"Job {job_id} cancelled.")
            return True

    def cancel_all_jobs(self):
        for job in self.job_log:
            self.cancel_job(job)
    
    def get_local_job_status(self, job):
        return self.job_log[job.job_id].job_status

    def check_for_response(self):
        inboundMessages = self.inbound_queue.receive_messages(MaxNumberOfMessages=10) ## TODO: Make AWScontroller method

        if len(inboundMessages) > 0:
            for message in inboundMessages:
                try:
                    job = self.load_from_json(message.body)
                    self.job_log[job.job_id] = job
                    if job.job_status == STATUS.COMPLETED and job.result_file is not None:
                        self.update_result_file(job)
                except Exception as e:
                    status_message = json.loads(message.body)
                    try:
                        report = status_message["report"]
                        instance_id = status_message["instance"]
                        self.aws_controller.remove_instance(instance_id)
                    except:
                        self.job_log[status_message["job_id"]].progress[0] = status_message["current"]
                        self.job_log[status_message["job_id"]].progress[1] = status_message["total"]
                        self.job_log[status_message["job_id"]].job_status = STATUS.RUNNING

                message.delete()
        
    def from_json(self, json_str):  
        return json.loads(json_str)
        
    def load_from_json(self, json_string):
        json_string = self.from_json(json_string)
        job = Job(int(json_string["job_id"]), json_string["hash"], json_string["hash_type"], self.convert_status(json_string["job_status"]), json_string["attack_mode"], json_string["required_info"])
        return job
    
    def update_result_file(self, job): # NOT WORKING!!!
        with open(job.result_file, "w") as f:
            strng = f"{job.hash} : {job.required_info}"
            f.write(strng)
    
    def convert_status(self, status):
        if status == 1:
            return STATUS.CREATED
        elif status == 2:
            return STATUS.RUNNING
        elif status == 3:
            return STATUS.COMPLETED
        elif status == 4:
            return STATUS.FAILED
        elif status == 5:
            return STATUS.CANCELLED
        elif status == 6:
            return STATUS.QUEUED
        elif status == 7:
            return STATUS.PENDING
        elif status == 8:
            return STATUS.EXHAUSTED
        else:
            return None

    def delete_job(self, job):
        del self.job_log[job.job_id]

class REQUEST(IntEnum):
    CANCEL = 1
    STATUS = 2
 

class Command:

    def __init__(self, job_id, request):
        self.job_id = job_id
        self.request = request

    def __str__(self):
        return f"Job ID: {self.job_id} | Job Status: {self.job_status}"
    
    def to_json(self):
        return json.dumps(self, default=lambda o: o.__dict__, sort_keys=True, indent=4)
    
    
        
        
    




        