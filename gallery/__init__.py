import inspect
import json
import os
import zipfile
import re
import subprocess

from sys import stderr

from alembic import command
import filetype
from csh_ldap import CSHLDAP
from flask import Flask
from flask import current_app
from flask import jsonify
from flask import redirect
from flask import request
from flask import url_for
from flask import render_template
from flask import session
from flask import send_from_directory
from flask_sqlalchemy import SQLAlchemy
from flask_pyoidc.flask_pyoidc import OIDCAuthentication
import flask_migrate
import requests
from werkzeug import secure_filename

app = Flask(__name__)
app.config['SQLALCHEMY_TRACK_NOTIFICATIONS'] = False


if os.path.exists(os.path.join(os.getcwd(), "config.py")):
    app.config.from_pyfile(os.path.join(os.getcwd(), "config.py"))
else:
    app.config.from_pyfile(os.path.join(os.getcwd(), "config.env.py"))

db = SQLAlchemy(app)
migrate = flask_migrate.Migrate(app, db)

# Disable SSL certificate verification warning
requests.packages.urllib3.disable_warnings()

app.config["GIT_REVISION"] = subprocess.check_output(['git',
                                                      'rev-parse',
                                                      '--short',
                                                      'HEAD']).decode('utf-8').rstrip()


auth = OIDCAuthentication(app,
                          issuer=app.config['OIDC_ISSUER'],
                          client_registration_info=app.config['OIDC_CLIENT_CONFIG'])


ldap = CSHLDAP(app.config['LDAP_BIND_DN'],
               app.config['LDAP_BIND_PW'])

# pylint: disable=C0413
from gallery.models import Directory
from gallery.models import File

from gallery.util import allowed_file
from gallery.util import get_dir_file_contents
from gallery.util import get_dir_tree_dict
from gallery.util import get_full_dir_path
from gallery.util import convert_bytes_to_utf8
from gallery.util import gallery_auth

from gallery.file_modules import parse_file_info
from gallery.file_modules import FileModule

import gallery.ldap as gallery_ldap

for func in inspect.getmembers(gallery_ldap):
    if func[0].startswith("ldap_"):
        unwrapped = inspect.unwrap(func[1])
        if inspect.isfunction(unwrapped):
            app.add_template_global(inspect.unwrap(unwrapped), name=func[0])

@app.route("/")
@auth.oidc_auth
def index():
    root_id = get_dir_tree(internal=True)
    return redirect("/view/dir/" + str(root_id['id']))

@app.route('/upload', methods=['GET'])
@auth.oidc_auth
@gallery_auth
def view_upload(auth_dict=None):
    return render_template("upload.html",
                            auth_dict=auth_dict)

@app.route('/upload', methods=['POST'])
@auth.oidc_auth
@gallery_auth
def upload_file(auth_dict=None):
    # Dropzone multi file is broke with .getlist()
    uploaded_files = [t[1] for t in request.files.items()]

    files = []
    owner = auth_dict['uuid']

    # hardcoding is bad
    parent = request.form.get('parent_id')

    # Create return object
    upload_status = {}
    upload_status['error'] = []
    upload_status['success'] = []
    upload_status['redirect'] = "/view/dir/" + str(parent)

    dir_path = get_full_dir_path(parent)
    for upload in uploaded_files:
        filename = secure_filename(upload.filename)
        file_model = File.query.filter(File.parent == parent) \
                               .filter(File.name == filename).first()
        if file_model is None:
            filepath = os.path.join(dir_path, filename)
            upload.save(filepath)

            file_model = add_file(filename, dir_path, parent, "", owner)
            if file_model is None:
                upload_status['error'].append(filename)
                continue
            upload_status['success'].append(
                {
                    "name": file_model.name,
                    "id": file_model.id
                })
        else:
            upload_status['error'].append(filename)

    refresh_thumbnail()
    # actually redirect to URL
    # change from FORM post to AJAX maybe?
    return jsonify(upload_status)

@app.route('/create_folder', methods=['GET'])
@auth.oidc_auth
@gallery_auth
def view_mkdir(auth_dict=None):
    return render_template("mkdir.html",
                            auth_dict=auth_dict)

@app.route('/api/mkdir', methods=['POST'])
@auth.oidc_auth
@gallery_auth
def api_mkdir(internal=False, parent_id=None, dir_name=None, owner=None,
              auth_dict=None):
    owner = auth_dict['uuid']

    # hardcoding is bad
    parent_id = request.form.get('parent_id')

    path = get_full_dir_path(parent_id)

    # at this point path is something like
    # gallery-data/root
    file_path = os.path.join(path, request.form.get('dir_name'))
    _, count = re.subn(r'[^a-zA-Z0-9 \/\-\_]', '', file_path)
    if not file_path.startswith("/gallery-data/root") or count != 0:
        return "invalid path" + file_path, 400

    # mkdir -p that shit
    if not os.path.exists(file_path):
        os.makedirs(file_path)

    # strip out new dir names now filtered by regex!
    if file_path.startswith(path):
        file_path = file_path[(len(path)):]


    upload_status = {}
    upload_status['error'] = []
    upload_status['success'] = []

    # Sometimes we want to put things in their place
    if file_path != "" and file_path != "/":
        path = file_path.split('/')
        path.pop(0) # remove blank

        # now put these dirs in the db
        for directory in path:
            # ignore dir//dir patterns
            if directory == "":
                continue
            parent_id = add_directory(parent_id, directory, "", owner)
            upload_status['success'].append(
                {
                    "name": directory,
                    "id": parent_id
                })

    # Create return object
    upload_status['redirect'] = "/view/dir/" + str(parent_id)
    return jsonify(upload_status)

# @route("/preload")
# @auth.oidc_auth
# def preload_images():
#     if not os.path.exists("/gallery-data"):
#         os.makedirs("/gallery-data")
#
#     r = requests.get("https://csh.rit.edu/~loothelion/test.zip")
#     with open("test.zip", "wb") as archive:
#         archive.write(r.content)
#
#     with zipfile.ZipFile("test.zip", "r") as zip_file:
#         zip_file.extractall("/gallery-data/")
#
#     return redirect(url_for("index"), 302)
#
@app.route("/refreshdb")
@auth.oidc_auth
def refresh_db():
    files = get_dir_tree_dict()
    check_for_dir_db_entry(files, '', None)
    return redirect(url_for("index"), 302)

def check_for_dir_db_entry(dictionary, path, parent_dir):
    uuid_thumbnail = "reedphoto.jpg"

    # check db for this path with parents shiggg
    dir_name = path.split('/')[-1]
    if dir_name == "":
        dir_name = "root"
    dir_model = None
    if parent_dir:
        dir_model = Directory.query.filter(Directory.name == dir_name) \
                                   .filter(Directory.parent == parent_dir.id).first()
    else:
        dir_model = Directory.query.filter(Directory.parent == None).first()

    if dir_model is None:
        # fuck go back this directory doesn't exist as a model
        # we gotta add this shit
        if parent_dir:
            dir_model = Directory(parent_dir.id, dir_name, "", "root",
                                  uuid_thumbnail, "{\"g\":[]}")
        else:
            dir_model = Directory(None, dir_name, "", "root",
                                  uuid_thumbnail, "{\"g\":[]}")
        db.session.add(dir_model)
        db.session.flush()
        db.session.commit()
        db.session.refresh(dir_model)

    # get directory class as dir_model
    for dir_p in dictionary:
        # Don't traverse local files
        if dir_p == '.':
            continue
        check_for_dir_db_entry(
            dictionary[dir_p],
            os.path.join(path, dir_p),
            dir_model)

    for file_p in dictionary['.']:
        # check db for this file path
        file_model = File.query.filter(File.parent == dir_model.id) \
                               .filter(File.name == file_p).first()
        if file_model is None:
            add_file(file_p, path, dir_model.id, "", "root")

def add_directory(parent_id, name, description, owner):
    uuid_thumbnail = "reedphoto.jpg"
    dir_model = Directory(parent_id, name, description, owner,
                          uuid_thumbnail, "{\"g\":[]}")
    db.session.add(dir_model)
    db.session.flush()
    db.session.commit()
    db.session.refresh(dir_model)

    return dir_model.id

def add_file(file_name, path, dir_id, description, owner):
    uuid_thumbnail = "reedphoto.jpg"

    file_path = os.path.join('/', path, file_name)

    #exif_dict = {'Exif':{}}
    #file_type = "Text"
    #if filetype.guess(file_path).mime == "image/x-canon-cr2":
    #    # wand convert from cr2 to jpeg remove cr2 file
    #    old_file_path = file_path
    #    file_path = os.path.splitext(file_path)[0]
    #    subprocess.check_output(['dcraw',
    #                             '-w',
    #                             old_file_path])
    #    subprocess.check_output(['convert',
    #                             file_path + ".ppm",
    #                             file_path + ".jpg"])
    #    # rm the old file
    #    os.remove(old_file_path)
    #    # rm the ppm transitional file
    #    os.remove(file_path + ".ppm")
    #    # final jpg
    #    file_path = file_path + ".jpg"

    #uuid_thumbnail = hash_file(file_path) + ".jpg"
    #file_type = "Photo"

    #elif is_video:
    #    file_type = "Video"

    file_data = parse_file_info(file_path)
    if file_data is None:
        return None

    file_model = File(dir_id, file_data.get_name(), description, owner,
                      file_data.get_thumbnail(), file_data.get_type(),
                      json.dumps(file_data.get_exif()))
    db.session.add(file_model)
    db.session.flush()
    db.session.commit()
    db.session.refresh(file_model)
    return file_model


@app.route("/refresh_thumbnails")
@auth.oidc_auth
def refresh_thumbnail():
    def refresh_thumbnail_helper(dir_model):
        dir_children = [d for d in Directory.query.filter(Directory.parent == dir_model.id).all()]
        file_children = [f for f in File.query.filter(File.parent == dir_model.id).all()]
        for file in file_children:
            if file.thumbnail_uuid != "reedphoto.jpg":
                return file.thumbnail_uuid
        for d in dir_children:
            if d.thumbnail_uuid != "reedphoto.jpg":
                return d.thumbnail_uuid
        # WE HAVE TO GO DEEPER (inception noise)
        for d in dir_children:
            return refresh_thumbnail_helper(d)
        # No thumbnail found
        return "reedphoto.jpg"

    missing_thumbnails = Directory.query.filter(Directory.thumbnail_uuid == "reedphoto.jpg").all()
    for dir_model in missing_thumbnails:
        dir_model.thumbnail_uuid = refresh_thumbnail_helper(dir_model)
        db.session.flush()
        db.session.commit()
        db.session.refresh(dir_model)
    return redirect('/view/dir/3')


# TODO implement me frontend
@app.route("/api/file/describe/<int:file_id>", methods=['POST'])
@auth.oidc_auth
@gallery_auth
def describe_file(file_id, auth_dict=None):
    file_id = int(file_id)
    file_model = File.query.filter(File.id == file_id).first()

    if file_model is None:
        return "file not found", 404

    if not (auth_dict['is_eboard']
            or auth_dict['is_rtp']
            or auth_dict['uuid'] == file_model.author):
        return "Permission denied", 403

    File.query.filter(File.id == file_id).update({
        'caption': request.form.get('caption')
    })
    db.session.flush()
    db.session.commit()

    return "ok", 200

# TODO implement me frontend
@app.route("/api/dir/describe/<int:dir_id>", methods=['POST'])
@auth.oidc_auth
@gallery_auth
def describe_dir(dir_id, auth_dict=None):
    dir_id = int(dir_id)
    dir_model = Directory.query.filter(Directory.id == dir_id).first()

    if dir_model is None:
        return "dir not found", 404

    if not (auth_dict['is_eboard']
            or auth_dict['is_rtp']
            or auth_dict['uuid'] == dir_model.author):
        return "Permission denied", 403

    Directory.query.filter(Directory.id == dir_id).update({
        'description': request.form.get('description')
    })
    db.session.flush()
    db.session.commit()

    return "ok", 200

@app.route("/api/file/get/<int:file_id>")
@auth.oidc_auth
def display_file(file_id):
    file_id = int(file_id)
    path_stack = []
    file_model = File.query.filter(File.id == file_id).first()

    if file_model is None:
        return "file not found", 404

    dir_model = Directory.query.filter(Directory.id == file_model.parent).first()

    path = get_full_dir_path(dir_model.id)

    return send_from_directory(path, file_model.name)

@app.route("/api/thumbnail/get/<int:file_id>")
@auth.oidc_auth
def display_thumbnail(file_id):
    file_id = int(file_id)
    file_model = File.query.filter(File.id == file_id).first()

    if file_model is None:
        return send_from_directory('/gallery-data/thumbnails', 'reedphoto.jpg')

    return send_from_directory('/gallery-data/thumbnails', file_model.thumbnail_uuid)

@app.route("/api/thumbnail/get/dir/<int:dir_id>")
@auth.oidc_auth
def display_dir_thumbnail(dir_id):
    dir_id = int(dir_id)
    dir_model = Directory.query.filter(Directory.id == dir_id).first()

    return send_from_directory('/gallery-data/thumbnails', dir_model.thumbnail_uuid)

@app.route("/api/file/next/<int:file_id>")
@auth.oidc_auth
def get_file_next_id(file_id, internal=False):
    file_id = int(file_id)
    file_model = File.query.filter(File.id == file_id).first()
    files = [f.id for f in get_dir_file_contents(file_model.parent)]

    idx = files.index(file_id) + 1

    if idx >= len(files):
        idx = -1
    else:
        idx = files[idx]

    if internal:
        return idx
    return jsonify({"index": idx})

@app.route("/api/file/prev/<int:file_id>")
@auth.oidc_auth
def get_file_prev_id(file_id, internal=False):
    file_id = int(file_id)
    file_model = File.query.filter(File.id == file_id).first()
    files = [f.id for f in get_dir_file_contents(file_model.parent)]

    idx = files.index(file_id) - 1

    if idx < 0:
        idx = -1
    else:
        idx = files[idx]

    if internal:
        return idx
    return jsonify({"index": idx})

@app.route("/api/get_dir_tree")
@auth.oidc_auth
def get_dir_tree(internal=False):
    def get_dir_children(dir_id):
        dirs = [d for d in Directory.query.filter(Directory.parent == dir_id).all()]
        children = []
        for child in dirs:
            children.append({
                'name': child.name,
                'id': child.id,
                'children': get_dir_children(child.id)
                })
        return children

    root = dir_model = Directory.query.filter(Directory.parent == None).first()

    tree = {}

    tree['name'] = root.name
    tree['id'] = root.id
    tree['children'] = get_dir_children(root.id)

    # Hardcode gallery name to not be root FIXME
    tree['children'][0]['children'][0]['name'] = "CSH Gallery"

    # return after gallery-data
    if internal:
        return tree['children'][0]['children'][0]
    else:
        return jsonify(tree['children'][0]['children'][0])

@app.route("/api/directory/get/<int:dir_id>")
@auth.oidc_auth
def display_files(dir_id, internal=False):
    dir_id = int(dir_id)
    file_list = [("File", f) for f in File.query.filter(File.parent == dir_id).all()]
    dir_list = [("Directory", d) for d in Directory.query.filter(Directory.parent == dir_id).all()]
    ret_dict = dir_list + file_list
    if internal:
        return ret_dict
    return jsonify(ret_dict)

@app.route("/view/dir/<int:dir_id>")
@auth.oidc_auth
@gallery_auth
def render_dir(dir_id, auth_dict=None):
    dir_id = int(dir_id)
    if dir_id < 3:
        return redirect('/view/dir/3')

    children = display_files(dir_id, internal=True)
    dir_model = Directory.query.filter(Directory.id == dir_id).first()
    description = dir_model.description
    display_description = len(description) > 0


    # Hardcode gallery name to not be root FIXME
    if dir_id == 3:
        dir_model.name = "CSH Gallery"

    display_parent = True
    if dir_model is None or dir_model.parent is None or dir_id == 3:
        display_parent = False
    path_stack = []
    path_stack.append(dir_model)
    while dir_model.parent is not None:
        dir_model = Directory.query.filter(Directory.id == dir_model.parent).first()
        path_stack.append(dir_model)
    path_stack.reverse()
    return render_template("view_dir.html",
                           children=children,
                           directory=dir_model,
                           parents=path_stack[2:],
                           display_parent=display_parent,
                           description=description,
                           display_description=display_description,
                           auth_dict=auth_dict)

@app.route("/view/file/<int:file_id>")
@auth.oidc_auth
@gallery_auth
def render_file(file_id, auth_dict=None):
    file_id = int(file_id)
    file_model = File.query.filter(File.id == file_id).first()
    description = file_model.caption
    display_description = len(description) > 0
    display_parent = True
    if file_model is None or file_model.parent is None:
        display_parent = False
    path_stack = []
    path_stack.append(file_model)
    dir_model = file_model
    while dir_model.parent is not None:
        dir_model = Directory.query.filter(Directory.id == dir_model.parent).first()
        path_stack.append(dir_model)
    path_stack.reverse()
    auth_dict['can_edit'] = (auth_dict['is_eboard'] or auth_dict['is_rtp'] or auth_dict['uuid'] == file_model.author)
    return render_template("view_file.html",
                           file_id=file_id,
                           file=file_model,
                           parent=file_model.parent,
                           parents=path_stack[2:],
                           next_file=get_file_next_id(file_id, internal=True),
                           prev_file=get_file_prev_id(file_id, internal=True),
                           display_parent=display_parent,
                           description=description,
                           display_description=display_description,
                           auth_dict=auth_dict)

@app.route("/logout")
@auth.oidc_logout
def logout():
    return redirect(url_for('index'), 302)
